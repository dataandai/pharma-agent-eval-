from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from src.actions.executor import ActionExecutor
from src.actions.proposals import build_proposal
from src.audit.log import AuditLog
from src.billing.reconciliation import reconcile_plan
from src.data.repositories import DataRepository, SandboxRepository
from src.domain.models import ApprovalDecision, AuditQuery, Intent
from src.graph.compat import END, START, MemorySaver, StateGraph
from src.graph.state import AgentState
from src.interpreter import default_interpreter
from src.knowledge import answer_key_concept


class AgentNodes:
    def __init__(self, root: Path, interpreter=None):
        self.root = root
        self.data = DataRepository(root)
        self.sandbox = SandboxRepository(root)
        self.executor = ActionExecutor(root)
        self.audit = AuditLog(root)
        self.interpreter = interpreter or default_interpreter()

    def classify(self, state: AgentState) -> dict[str, Any]:
        decision = self.interpreter.classify(state["user_message"])
        return {
            "intent": decision.intent.value,
            "_decision": decision.model_dump(mode="json"),
            "error": None,
        }

    def route(self, state: AgentState) -> str:
        return state["intent"]

    def investigate(self, state: AgentState) -> dict[str, Any]:
        decision = state["_decision"]
        plan_id = decision.get("referenced_plan_id") or state.get("active_plan_id")
        if not plan_id:
            return self._respond(state, "Please provide a plan ID such as P-100.", error="missing_plan_id")
        try:
            plan, rows, findings = reconcile_plan(self.data, plan_id)
            proposals = []
            for finding in findings:
                proposal = build_proposal(finding, plan, self.sandbox)
                if proposal:
                    proposals.append(proposal)
                    self.audit.append(
                        thread_id=state["thread_id"],
                        event_type="PROPOSAL_CREATED",
                        action_id=proposal.action_id,
                        action_type=proposal.action_type.value,
                        plan_id=proposal.plan_id,
                        finding_id=proposal.source_finding_id,
                        amount=proposal.amount,
                        currency=proposal.currency,
                        status=proposal.status.value,
                        summary=f"Created proposal {proposal.action_id} for {proposal.action_type.value}.",
                    )
            lines = [
                f"Plan **{plan.plan_id}** bills **{plan.amount} {plan.currency}** on a **{plan.cadence}** cadence.",
            ]
            if not findings:
                lines.append("No billing anomalies were found.")
            else:
                lines.append(f"Found **{len(findings)}** finding(s):")
                for finding in findings:
                    lines.append(
                        f"- `{finding.finding_id}` — **{finding.anomaly_type.value}**: {finding.summary}\n"
                        f"  Calculation: `{finding.evidence.expression}`"
                    )
                if proposals:
                    lines.append("\nPending proposals:")
                    for proposal in proposals:
                        amount = f" {proposal.amount} {proposal.currency}" if proposal.amount is not None else ""
                        lines.append(
                            f"- `{proposal.action_id}` — **{proposal.action_type.value}**{amount}. "
                            f"Approval is required before execution."
                        )
            self.audit.append(
                thread_id=state["thread_id"],
                event_type="INVESTIGATION_COMPLETED",
                plan_id=plan.plan_id,
                customer_id=plan.customer_id,
                status="completed",
                summary=f"Investigated {plan.plan_id}; found {len(findings)} findings.",
            )
            update = {
                "active_plan_id": plan.plan_id,
                "plan": plan.model_dump(mode="json"),
                "reconciliation_rows": [row.model_dump(mode="json") for row in rows],
                "findings": [finding.model_dump(mode="json") for finding in findings],
                "pending_actions": [proposal.model_dump(mode="json") for proposal in proposals],
            }
            return {**update, **self._respond(state, "\n".join(lines))}
        except Exception as exc:
            return self._respond(state, f"Investigation failed: {exc}", error=str(exc))

    def knowledge(self, state: AgentState) -> dict[str, Any]:
        return self._respond(state, answer_key_concept(state["user_message"]))

    def follow_up(self, state: AgentState) -> dict[str, Any]:
        if not state.get("plan"):
            return self._respond(state, "There is no active plan in the conversation yet.", error="no_active_plan")
        text = state["user_message"].lower()
        plan = state["plan"]
        if "currency" in text or "deviza" in text:
            response = f"Plan **{plan['plan_id']}** is denominated in **{plan['currency']}**."
        elif "other months" in text or "többi hónap" in text:
            rows = state.get("reconciliation_rows", [])
            response = "\n".join(
                f"- {row['period_start']} to {row['period_end']}: expected {row['expected_amount']} {row['currency']}, "
                f"net billed {row['net_billed_amount']}, delta {row['delta']}."
                for row in rows
            )
        elif "why" in text or "miért" in text:
            findings = state.get("findings", [])
            response = "\n".join(
                f"- {finding['summary']} Calculation: `{finding['evidence']['expression']}`"
                for finding in findings
            ) or "No previous finding is available."
        else:
            response = f"The active plan is {plan['plan_id']}."
        return self._respond(state, response)

    def approve(self, state: AgentState) -> dict[str, Any]:
        decision = state["_decision"]
        action_id = decision.get("referenced_action_id")
        pending = self.sandbox.pending_proposals()
        if not action_id:
            if len(pending) != 1:
                return self._respond(
                    state,
                    "Approval is ambiguous. Specify an action ID, for example: `approve ACT-...`.",
                    error="ambiguous_approval",
                )
            action_id = pending[0].action_id
        proposal = self.sandbox.get_proposal(action_id)
        if proposal is None:
            return self._respond(state, f"Unknown proposal {action_id}.", error="unknown_action")
        try:
            result = self.executor.approve_and_apply(
                ApprovalDecision(
                    action_id=proposal.action_id,
                    proposal_hash=proposal.proposal_hash,
                    decision="approve",
                ),
                thread_id=state["thread_id"],
            )
            applied = list(state.get("applied_actions", []))
            if action_id not in applied:
                applied.append(action_id)
            return {
                "applied_actions": applied,
                "pending_actions": [
                    row for row in state.get("pending_actions", []) if row["action_id"] != action_id
                ],
                **self._respond(state, result.message),
            }
        except Exception as exc:
            return self._respond(state, f"Action was not applied: {exc}", error=str(exc))

    def reject(self, state: AgentState) -> dict[str, Any]:
        decision = state["_decision"]
        action_id = decision.get("referenced_action_id")
        pending = self.sandbox.pending_proposals()
        if not action_id and len(pending) == 1:
            action_id = pending[0].action_id
        if not action_id:
            return self._respond(state, "Specify which action to reject.", error="ambiguous_rejection")
        proposal = self.sandbox.get_proposal(action_id)
        if not proposal:
            return self._respond(state, f"Unknown proposal {action_id}.", error="unknown_action")
        result = self.executor.approve_and_apply(
            ApprovalDecision(action_id=action_id, proposal_hash=proposal.proposal_hash, decision="reject"),
            thread_id=state["thread_id"],
        )
        return self._respond(state, result.message)

    def rollback(self, state: AgentState) -> dict[str, Any]:
        action_id = state["_decision"].get("referenced_action_id")
        if not action_id and len(state.get("applied_actions", [])) == 1:
            action_id = state["applied_actions"][0]
        if not action_id:
            return self._respond(state, "Specify the applied action ID to roll back.", error="ambiguous_rollback")
        try:
            result = self.executor.rollback(action_id, thread_id=state["thread_id"])
            return self._respond(state, result.message)
        except Exception as exc:
            return self._respond(state, f"Rollback failed: {exc}", error=str(exc))

    def audit_query(self, state: AgentState) -> dict[str, Any]:
        text = state["user_message"]
        plan_match = re.search(r"\bP-\d+\b", text, re.IGNORECASE)
        action_match = re.search(r"\bACT-[A-Z0-9]+\b", text, re.IGNORECASE)
        query = AuditQuery(
            plan_id=plan_match.group(0).upper() if plan_match else state.get("active_plan_id"),
            action_id=action_match.group(0).upper() if action_match else None,
        )
        events = self.audit.query(query)
        if not events:
            return self._respond(state, "No matching audit events were found.")
        response = "\n".join(
            f"- {event.timestamp.isoformat()} — **{event.event_type}** — {event.summary}"
            for event in events
        )
        return self._respond(state, response)

    def unknown(self, state: AgentState) -> dict[str, Any]:
        return self._respond(
            state,
            "Try `investigate P-100`, ask about a Key Concept, approve a proposal, query audit history, or roll back an action.",
        )

    def _respond(self, state: AgentState, response: str, error: str | None = None) -> dict[str, Any]:
        messages = list(state.get("messages", []))
        if not messages or messages[-1] != {"role": "user", "content": state["user_message"]}:
            messages.append({"role": "user", "content": state["user_message"]})
        messages.append({"role": "assistant", "content": response})
        return {"messages": messages, "response": response, "error": error}


def build_agent_graph(root: Path, interpreter=None):
    nodes = AgentNodes(root, interpreter=interpreter)
    graph = StateGraph(AgentState)
    graph.add_node("classify", nodes.classify)
    graph.add_node("investigate", nodes.investigate)
    graph.add_node("knowledge", nodes.knowledge)
    graph.add_node("follow_up", nodes.follow_up)
    graph.add_node("approve", nodes.approve)
    graph.add_node("reject", nodes.reject)
    graph.add_node("rollback", nodes.rollback)
    graph.add_node("audit_query", nodes.audit_query)
    graph.add_node("unknown", nodes.unknown)

    graph.add_edge(START, "classify")
    graph.add_conditional_edges(
        "classify",
        nodes.route,
        {
            Intent.INVESTIGATE.value: "investigate",
            Intent.KNOWLEDGE_QUESTION.value: "knowledge",
            Intent.FOLLOW_UP.value: "follow_up",
            Intent.APPROVE_ACTION.value: "approve",
            Intent.REJECT_ACTION.value: "reject",
            Intent.ROLLBACK_ACTION.value: "rollback",
            Intent.AUDIT_QUERY.value: "audit_query",
            Intent.UNKNOWN.value: "unknown",
        },
    )
    for node in ["investigate", "knowledge", "follow_up", "approve", "reject", "rollback", "audit_query", "unknown"]:
        graph.add_edge(node, END)
    return graph.compile(checkpointer=MemorySaver())


def initial_state(thread_id: str = "demo-thread") -> AgentState:
    return AgentState(
        thread_id=thread_id,
        messages=[],
        active_plan_id=None,
        plan=None,
        reconciliation_rows=[],
        findings=[],
        pending_actions=[],
        applied_actions=[],
        response="",
        error=None,
    )


def run_turn(graph, state: AgentState, message: str) -> AgentState:
    next_state = dict(state)
    next_state["user_message"] = message
    return graph.invoke(
        next_state,
        config={"configurable": {"thread_id": state["thread_id"]}},
    )
