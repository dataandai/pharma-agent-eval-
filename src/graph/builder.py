"""The graph, with two tool tiers and a real interrupt gate.

```
START -> classify
  classify --route--> review | follow_up | knowledge | audit_query | unknown -> END
  classify --route--> gate
  gate --confirmed?--> apply -> END
  gate --refused-----> END
```

**Two tiers.** Reading and drafting run freely: `review` loads the study, runs
every detector and drafts corrective actions, and none of that persists anything
outside the proposals ledger. Writing is gated.

**One inbound edge.** `apply` is the only node that mutates a ledger, and `gate`
is its only predecessor. There is no path from `classify` to a write. That is
checked structurally by a test rather than asserted in a comment.

**The gate really pauses.** It calls `interrupt()` with the approval card and
waits for a human turn. It does not pre-decide and it does not proceed on
silence.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.actions import ActionExecutor, check_confirmation, draft_actions
from src.audit_trail import AuditTrail
from src.detectors import run_all
from src.findings import Family
from src.graph.compat import END, HAS_INTERRUPT, MemorySaver, StateGraph, interrupt
from src.graph.state import AgentState
from src.interpreter import Intent, default_interpreter
from src.knowledge import answer_key_concept
from src.protocol import governing_version
from src.sandbox import Sandbox
from src.study import Study
from src.verdicts import Verdict

WRITE_NODE = "apply"
GATE_NODE = "gate"


class AgentNodes:
    def __init__(self, root: Path, interpreter=None, data_dir: Path | None = None):
        self.root = Path(root)
        self.data_dir = Path(data_dir) if data_dir else self.root / "data"
        self.sandbox = Sandbox(self.root)
        self.executor = ActionExecutor(self.root)
        self.audit = AuditTrail(self.root)
        self.interpreter = interpreter or default_interpreter()

    # -- tier 1: read and draft, nothing persisted outside proposals --------

    def classify(self, state: AgentState) -> dict[str, Any]:
        decision = self.interpreter.classify(state.get("user_message", ""))
        return {
            "intent": decision.intent.value,
            "_decision": {
                "subject_id": decision.subject_id,
                "site_id": decision.site_id,
                "action_id": decision.action_id,
            },
            "error": None,
        }

    def route(self, state: AgentState) -> str:
        intent = state.get("intent", Intent.UNKNOWN.value)
        if intent in (Intent.APPROVE_ACTION.value, Intent.REJECT_ACTION.value,
                      Intent.ROLLBACK_ACTION.value):
            return GATE_NODE
        return intent

    def review(self, state: AgentState) -> dict[str, Any]:
        decision = state.get("_decision", {})
        # A newly named subject or site replaces the conversational context
        # rather than merging with it. Falling back to the previous subject
        # when the user names a site answers a question nobody asked.
        if decision.get("subject_id") or decision.get("site_id"):
            subject_id = decision.get("subject_id")
            site_id = decision.get("site_id")
        else:
            subject_id = state.get("active_subject_id")
            site_id = state.get("active_site_id")

        study = Study.load(self.data_dir)
        findings = run_all(study)

        if subject_id:
            findings = [f for f in findings if f.subject_id == subject_id]
        elif site_id:
            findings = [f for f in findings if f.site_id == site_id]

        reportable = [f for f in findings if f.is_reportable]
        versions = {
            s.subject_id: governing_version(s, study.catalogue).version.label
            for s in study.subjects.values()
        }
        drafts = draft_actions(reportable, self.sandbox, versions)
        self.executor.save_drafts(drafts)

        self.audit.append(
            thread_id=state["thread_id"], event_type="REVIEW_COMPLETED",
            summary=(f"Reviewed {subject_id or site_id or 'the whole study'}: "
                     f"{len(reportable)} reportable finding(s), "
                     f"{len(drafts)} action(s) drafted. Nothing written."),
            subject_id=subject_id, site_id=site_id,
        )

        return {
            "active_subject_id": subject_id,
            "active_site_id": site_id,
            "findings": [f.to_dict() for f in findings],
            "pending_actions": [d.to_dict() for d in drafts],
            **self._respond(state, _render_review(findings, drafts,
                                                  subject_id or site_id)),
        }

    def follow_up(self, state: AgentState) -> dict[str, Any]:
        findings = state.get("findings", [])
        if not findings:
            return self._respond(state, "Nothing has been reviewed in this "
                                        "conversation yet. Try `review S-004`.",
                                 error="no_active_review")
        text = (state.get("user_message") or "").lower()

        if "why" in text or "miért" in text:
            body = "\n\n".join(
                f"- `{f['finding_id']}` {f['calculation']}" for f in findings[:5]
            )
        elif "version" in text or "verzió" in text:
            body = "\n".join(
                f"- `{f['finding_id']}` {f['subject_id']}: {f['calculation'][:200]}"
                for f in findings[:5]
            )
        elif "not assessable" in text or "unassessable" in text:
            rows = [f for f in findings if f["verdict"] == Verdict.NOT_ASSESSABLE.value]
            body = ("\n".join(f"- `{f['finding_id']}` {f['subject_id']}/{f['visit_id']}: "
                              f"{f['calculation'][:180]}" for f in rows)
                    or "Every record in this review could be assessed.")
        else:
            body = _summarise(findings)
        return self._respond(state, body)

    def knowledge_question(self, state: AgentState) -> dict[str, Any]:
        return self._respond(state, answer_key_concept(state.get("user_message", "")))

    def audit_query(self, state: AgentState) -> dict[str, Any]:
        decision = state.get("_decision", {})
        events = self.audit.query(
            subject_id=decision.get("subject_id") or state.get("active_subject_id"),
        ) or self.audit.events()
        if not events:
            return self._respond(state, "The audit trail is empty.")
        intact, problem = self.audit.verify_chain()
        lines = [
            f"- {e['timestamp'][:19]} **{e['event_type']}** {e['summary']}"
            + (f"\n  approved by {e['approved_by']}, verbatim: "
               f"{e['approval_verbatim']!r}" if e.get("approval_verbatim") else "")
            for e in events[-12:]
        ]
        chain = ("Hash chain intact." if intact
                 else f"**Hash chain broken**: {problem}")
        return self._respond(state, "\n".join(lines) + f"\n\n{chain}")

    def unknown(self, state: AgentState) -> dict[str, Any]:
        return self._respond(state, (
            "Try `review S-004`, `review SITE-02`, ask what an important deviation is, "
            "approve or reject a drafted action, ask what happened, or roll one back."
        ))

    # -- the gate ----------------------------------------------------------

    def gate(self, state: AgentState) -> dict[str, Any]:
        """Halts the graph and surfaces the pending decision.

        Nothing downstream of here runs until a human answers, and what they
        typed is carried forward verbatim into the audit trail.
        """
        decision = state.get("_decision", {})
        intent = state.get("intent")
        kind = {"approve_action": "approve", "reject_action": "reject",
                "rollback_action": "rollback"}[intent]

        action_id = decision.get("action_id")
        pending = [d for d in state.get("pending_actions", [])
                   if d.get("status") == "proposed"]
        if not action_id:
            if len(pending) != 1:
                return {
                    "gate_passed": False, "gate_decision": kind,
                    "gate_action_id": None,
                    **self._respond(state, (
                        f"{len(pending)} actions are pending, so `{kind}` is ambiguous. "
                        f"Name the action, for example `{kind} "
                        f"{pending[0]['action_id'] if pending else 'ACT-...'}`."
                    ), error="ambiguous_approval"),
                }
            action_id = pending[0]["action_id"]

        draft = self.executor.get_draft(action_id)
        if draft is None:
            return {"gate_passed": False, "gate_decision": kind,
                    "gate_action_id": action_id,
                    **self._respond(state, f"Unknown action {action_id}.",
                                    error="unknown_action")}

        if kind in ("reject", "rollback"):
            return {"gate_passed": True, "gate_decision": kind,
                    "gate_action_id": action_id,
                    "gate_verbatim": state.get("user_message", "")}

        card = approval_card(draft)
        if HAS_INTERRUPT:
            verbatim = interrupt(card)
        else:  # pragma: no cover - only without langgraph
            verbatim = state.get("user_message", "")

        accepted, reason = check_confirmation(draft, str(verbatim))
        if not accepted:
            return {"gate_passed": False, "gate_decision": kind,
                    "gate_action_id": action_id, "gate_verbatim": str(verbatim),
                    **self._respond(state, f"Not applied. {reason}",
                                    error="not_confirmed")}
        return {"gate_passed": True, "gate_decision": kind,
                "gate_action_id": action_id, "gate_verbatim": str(verbatim)}

    def gate_route(self, state: AgentState) -> str:
        return WRITE_NODE if state.get("gate_passed") else END

    # -- tier 2: the only node that writes ---------------------------------

    def apply(self, state: AgentState) -> dict[str, Any]:
        action_id = state["gate_action_id"]
        verbatim = state.get("gate_verbatim") or ""
        actor = state.get("actor", "unidentified user")
        kind = state.get("gate_decision")

        if kind == "reject":
            outcome = self.executor.reject(action_id, verbatim=verbatim,
                                           actor=actor, thread_id=state["thread_id"])
        elif kind == "rollback":
            outcome = self.executor.rollback(action_id, verbatim=verbatim,
                                             actor=actor, thread_id=state["thread_id"])
        else:
            outcome = self.executor.approve_and_apply(
                action_id, verbatim=verbatim, actor=actor,
                thread_id=state["thread_id"])

        applied = list(state.get("applied_actions", []))
        if outcome.ok and outcome.status == "applied" and action_id not in applied:
            applied.append(action_id)

        return {
            "applied_actions": applied,
            "pending_actions": [d for d in state.get("pending_actions", [])
                                if d["action_id"] != action_id],
            **self._respond(state, outcome.message,
                            error=None if outcome.ok else outcome.status),
        }

    # -- helper ------------------------------------------------------------

    def _respond(self, state: AgentState, response: str,
                 error: str | None = None) -> dict[str, Any]:
        messages = list(state.get("messages", []))
        user_turn = {"role": "user", "content": state.get("user_message", "")}
        if not messages or messages[-1] != user_turn:
            messages.append(user_turn)
        messages.append({"role": "assistant", "content": response})
        return {"messages": messages, "response": response, "error": error}


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def approval_card(draft) -> dict[str, Any]:
    """What the human is shown before anything is written."""
    return {
        "action_id": draft.action_id,
        "action": draft.action_type,
        "subject": draft.subject_id,
        "site": draft.site_id,
        "visit": draft.visit_id,
        "protocol_version_governing_subject": draft.governing_version,
        "calculation": draft.calculation,
        "proposed_classification": draft.proposed_classification,
        "classification_reasoning": draft.classification_reasoning,
        "classification_status": "PROPOSAL — the investigator decides, not this system",
        "evidence": list(draft.evidence),
        "will_write_to": f"sandbox/{draft.target_ledger}.json",
        "confirmation_required": draft.confirmation_level,
        "required_token": draft.required_token,
    }


def _summarise(findings: list[dict]) -> str:
    if not findings:
        return "No findings."
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding["verdict"]] = counts.get(finding["verdict"], 0) + 1
    return ("Verdicts: "
            + ", ".join(f"{count} {verdict}" for verdict, count in sorted(counts.items()))
            + ".")


def _render_review(findings, drafts, scope) -> str:
    if not findings:
        return (f"No findings for {scope or 'the study'}. "
                f"Every record that could be assessed conforms to the protocol.")

    deviations = [f for f in findings
                  if f.verdict is Verdict.DEVIATION and f.is_reportable]
    unassessable = [f for f in findings if f.verdict is Verdict.NOT_ASSESSABLE]
    suppressed = [f for f in findings if f.is_suppressed]

    lines = [
        f"**{scope or 'Study'} review** — {len(deviations)} deviation(s), "
        f"{len(unassessable)} record(s) I cannot assess, "
        f"{len(suppressed)} finding(s) subsumed or already logged.",
        "",
    ]
    for finding in deviations[:8]:
        family = ("data quality" if finding.family is Family.DATA_QUALITY
                  else "protocol deviation")
        lines.append(f"- `{finding.finding_id}` **{finding.detector}** ({family}) — "
                     f"{finding.subject_id or ''} {finding.visit_id or ''}")
        lines.append(f"  {finding.calculation}")
        if finding.classification:
            lines.append(f"  *Proposed classification:* "
                         f"**{finding.classification.proposed}** — "
                         f"{finding.classification.reasoning}")
    if unassessable:
        lines.append("")
        lines.append(f"**Not assessable ({len(unassessable)})** — these route to a site "
                     f"query, not to the deviation log:")
        for finding in unassessable[:6]:
            lines.append(f"- `{finding.finding_id}` {finding.subject_id or '(no subject)'}"
                         f"/{finding.visit_id or '-'}: {finding.calculation[:200]}")
    if suppressed:
        lines.append("")
        for finding in suppressed[:5]:
            lines.append(f"- `{finding.finding_id}` suppressed "
                         f"({finding.suppressed_by}) — not filed again.")
    if drafts:
        lines.append("")
        lines.append(f"**{len(drafts)} action(s) drafted, none written.**")
        for draft in drafts[:8]:
            token = (f" — requires the exact token `{draft.required_token}`"
                     if draft.needs_exact_token else "")
            lines.append(f"- `{draft.action_id}` {draft.action_type} "
                         f"-> sandbox/{draft.target_ledger}.json{token}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------

def build_agent_graph(root: Path, interpreter=None, data_dir: Path | None = None):
    nodes = AgentNodes(root, interpreter=interpreter, data_dir=data_dir)
    graph = StateGraph(AgentState)

    graph.add_node("classify", nodes.classify)
    graph.add_node("review", nodes.review)
    graph.add_node("follow_up", nodes.follow_up)
    graph.add_node("knowledge_question", nodes.knowledge_question)
    graph.add_node("audit_query", nodes.audit_query)
    graph.add_node("unknown", nodes.unknown)
    graph.add_node(GATE_NODE, nodes.gate)
    graph.add_node(WRITE_NODE, nodes.apply)

    from src.graph.compat import START
    graph.add_edge(START, "classify")
    graph.add_conditional_edges("classify", nodes.route, {
        Intent.REVIEW.value: "review",
        Intent.FOLLOW_UP.value: "follow_up",
        Intent.KNOWLEDGE_QUESTION.value: "knowledge_question",
        Intent.AUDIT_QUERY.value: "audit_query",
        Intent.UNKNOWN.value: "unknown",
        GATE_NODE: GATE_NODE,
    })
    # The gate is the write node's only predecessor.
    graph.add_conditional_edges(GATE_NODE, nodes.gate_route,
                                {WRITE_NODE: WRITE_NODE, END: END})
    for node in ("review", "follow_up", "knowledge_question", "audit_query",
                 "unknown", WRITE_NODE):
        graph.add_edge(node, END)

    return graph.compile(checkpointer=MemorySaver())


def initial_state(thread_id: str = "demo-thread",
                  actor: str = "Dr A. Kovacs (Principal Investigator)") -> AgentState:
    return AgentState(
        thread_id=thread_id, actor=actor, messages=[],
        active_subject_id=None, active_site_id=None,
        findings=[], pending_actions=[], applied_actions=[],
        gate_action_id=None, gate_decision=None, gate_passed=False,
        gate_verbatim=None, response="", error=None,
    )


def run_turn(graph, state: AgentState, message: str) -> AgentState:
    next_state = dict(state)
    next_state["user_message"] = message
    result = graph.invoke(next_state,
                          config={"configurable": {"thread_id": state["thread_id"]}})
    return _merge(state, result)


def resume_turn(graph, state: AgentState, reply: str) -> AgentState:
    """Answer a paused gate. This is the only way past it."""
    from src.graph.compat import Command
    result = graph.invoke(Command(resume=reply),
                          config={"configurable": {"thread_id": state["thread_id"]}})
    return _merge(state, result)


def _merge(previous: AgentState, result: dict) -> AgentState:
    merged = dict(previous)
    merged.update(result or {})
    return merged  # type: ignore[return-value]


def pending_interrupt(graph, state: AgentState) -> dict | None:
    """The approval card the graph is paused on, if any."""
    snapshot = graph.get_state({"configurable": {"thread_id": state["thread_id"]}})
    tasks = getattr(snapshot, "tasks", ()) or ()
    for task in tasks:
        for value in (getattr(task, "interrupts", ()) or ()):
            return getattr(value, "value", None)
    return None
