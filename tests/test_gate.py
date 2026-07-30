"""Phase 5: the gate, the write path, and the audit trail.

The structural test is the one that matters: `apply` is the only node that
mutates a ledger, and `gate` is its only predecessor. A comment claiming that is
worth nothing; this reads the compiled graph.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.actions import ActionExecutor, check_confirmation, draft_actions
from src.audit_trail import AuditTrail
from src.detectors import run_all
from src.graph.builder import (
    GATE_NODE,
    WRITE_NODE,
    build_agent_graph,
    initial_state,
    pending_interrupt,
    resume_turn,
    run_turn,
)
from src.sandbox import Sandbox
from src.study import Study

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def agent(project_root, interpreter):
    graph = build_agent_graph(project_root, interpreter=interpreter,
                             data_dir=project_root / "data")
    return graph, project_root


def drafts_for(project_root, subject_id):
    study = Study.load(project_root / "data")
    findings = [f for f in run_all(study)
                if f.subject_id == subject_id and f.is_reportable]
    return draft_actions(findings, Sandbox(project_root))


# --------------------------------------------------------------------------
# The structural invariant
# --------------------------------------------------------------------------

def test_the_write_node_is_reachable_only_from_the_gate(project_root, interpreter):
    graph = build_agent_graph(project_root, interpreter=interpreter)
    drawn = graph.get_graph()

    inbound = [edge.source for edge in drawn.edges if edge.target == WRITE_NODE]
    assert inbound == [GATE_NODE], (
        f"{WRITE_NODE} must have exactly one inbound edge, from {GATE_NODE}; "
        f"found {inbound}"
    )


def test_no_other_node_can_reach_the_write_node(project_root, interpreter):
    """Walk every path from START. The only route into `apply` goes via `gate`."""
    graph = build_agent_graph(project_root, interpreter=interpreter)
    drawn = graph.get_graph()

    outgoing: dict[str, list[str]] = {}
    for edge in drawn.edges:
        outgoing.setdefault(edge.source, []).append(edge.target)

    bad_paths = []
    stack = [("__start__", [])]
    seen = set()
    while stack:
        node, path = stack.pop()
        if node == WRITE_NODE:
            if GATE_NODE not in path:
                bad_paths.append(path + [node])
            continue
        if node in seen:
            continue
        seen.add(node)
        for nxt in outgoing.get(node, []):
            stack.append((nxt, path + [node]))
    assert bad_paths == []


# --------------------------------------------------------------------------
# Graduated confirmation
# --------------------------------------------------------------------------

def test_a_routine_action_takes_a_plain_affirmative(project_root):
    routine = [d for d in drafts_for(project_root, "S-013")
               if not d.needs_exact_token]
    assert routine
    accepted, _ = check_confirmation(routine[0], "yes")
    assert accepted


def test_an_important_deviation_needs_the_exact_token(project_root):
    """Classifying something as an important deviation reaches the IRB/EC."""
    important = [d for d in drafts_for(project_root, "S-006")
                 if d.action_type == "log_deviation"
                 and d.proposed_classification == "important"]
    assert important
    draft = important[0]

    assert draft.needs_exact_token
    assert check_confirmation(draft, "yes")[0] is False
    assert check_confirmation(draft, "approve")[0] is False
    assert check_confirmation(draft, draft.required_token)[0] is True


def test_escalation_needs_the_exact_token(project_root):
    """An escalation starts a clock."""
    escalations = [d for d in drafts_for(project_root, "S-006")
                   if d.action_type == "escalate_to_medical_monitor"]
    assert escalations
    assert escalations[0].needs_exact_token
    assert check_confirmation(escalations[0], "ok")[0] is False


def test_the_refusal_explains_why_the_token_is_needed(project_root):
    escalations = [d for d in drafts_for(project_root, "S-006")
                   if d.action_type == "escalate_to_medical_monitor"]
    _, reason = check_confirmation(escalations[0], "yes")
    assert "regulatory effect" in reason
    assert escalations[0].required_token in reason


# --------------------------------------------------------------------------
# Nothing is written without passing the gate
# --------------------------------------------------------------------------

def test_a_review_writes_nothing_to_any_ledger(agent):
    graph, root = agent
    run_turn(graph, initial_state("t1"), "review S-004")
    sandbox = Sandbox(root)
    for ledger in ("deviation_entries", "site_queries", "capas",
                   "amendment_proposals", "escalations"):
        assert sandbox.read(ledger) == []
    assert sandbox.version() == 0
    assert sandbox.read("proposals")   # drafted, not applied


def test_the_gate_pauses_and_surfaces_the_card(agent):
    graph, root = agent
    state = run_turn(graph, initial_state("t2"), "review S-013")
    action_id = state["pending_actions"][0]["action_id"]

    state = run_turn(graph, state, f"approve {action_id}")
    card = pending_interrupt(graph, state)

    assert card is not None, "the graph did not pause at the gate"
    assert card["action_id"] == action_id
    assert card["will_write_to"].startswith("sandbox/")
    assert "PROPOSAL" in card["classification_status"]
    assert Sandbox(root).version() == 0     # still nothing written


def test_resuming_with_an_affirmative_writes_exactly_one_record(agent):
    graph, root = agent
    state = run_turn(graph, initial_state("t3"), "review S-013")
    routine = next(d for d in state["pending_actions"]
                   if d["confirmation_level"] == "plain")

    state = run_turn(graph, state, f"approve {routine['action_id']}")
    state = resume_turn(graph, state, "yes")

    sandbox = Sandbox(root)
    ledger = sandbox.read(routine["target_ledger"])
    assert len(ledger) == 1
    assert ledger[0]["action_id"] == routine["action_id"]
    assert sandbox.version() == 1


def test_resuming_with_a_plain_yes_on_a_token_action_writes_nothing(agent):
    graph, root = agent
    state = run_turn(graph, initial_state("t4"), "review S-006")
    token_action = next(d for d in state["pending_actions"]
                        if d["confirmation_level"] == "token")

    state = run_turn(graph, state, f"approve {token_action['action_id']}")
    state = resume_turn(graph, state, "yes")

    assert Sandbox(root).read(token_action["target_ledger"]) == []
    assert Sandbox(root).version() == 0
    assert "exact confirmation token" in state["response"]


# --------------------------------------------------------------------------
# Audit trail
# --------------------------------------------------------------------------

def test_the_audit_entry_records_everything_an_inspector_needs(agent):
    graph, root = agent
    state = run_turn(graph, initial_state("t5"), "review S-006")
    draft = next(d for d in state["pending_actions"]
                 if d["action_type"] == "log_deviation")

    state = run_turn(graph, state, f"approve {draft['action_id']}")
    state = resume_turn(graph, state, draft["required_token"])

    applied = [e for e in AuditTrail(root).events()
               if e["event_type"] == "ACTION_APPLIED"]
    assert len(applied) == 1
    event = applied[0]

    assert event["finding_id"]
    assert event["proposed_classification"]
    assert event["classification_reasoning"]
    assert event["calculation"]
    assert event["approved_by"]
    assert event["approval_verbatim"] == draft["required_token"]
    assert event["sandbox_version_before"] == 0
    assert event["sandbox_version_after"] == 1


def test_the_audit_chain_is_verifiable(agent):
    graph, root = agent
    state = run_turn(graph, initial_state("t6"), "review S-013")
    routine = next(d for d in state["pending_actions"]
                   if d["confirmation_level"] == "plain")
    state = run_turn(graph, state, f"approve {routine['action_id']}")
    resume_turn(graph, state, "yes")

    intact, problem = AuditTrail(root).verify_chain()
    assert intact, problem


def test_a_tampered_audit_entry_breaks_the_chain(agent):
    graph, root = agent
    state = run_turn(graph, initial_state("t7"), "review S-013")
    routine = next(d for d in state["pending_actions"]
                   if d["confirmation_level"] == "plain")
    state = run_turn(graph, state, f"approve {routine['action_id']}")
    resume_turn(graph, state, "yes")

    path = root / "sandbox" / "audit_log.json"
    rows = json.loads(path.read_text(encoding="utf-8"))
    rows[-1]["approval_verbatim"] = "definitely approved"
    path.write_text(json.dumps(rows), encoding="utf-8")

    intact, problem = AuditTrail(root).verify_chain()
    assert intact is False and problem


# --------------------------------------------------------------------------
# Idempotency, staleness, rollback
# --------------------------------------------------------------------------

def test_applying_twice_is_idempotent(project_root):
    executor = ActionExecutor(project_root)
    draft = next(d for d in drafts_for(project_root, "S-013")
                 if not d.needs_exact_token)
    executor.save_drafts([draft])

    first = executor.approve_and_apply(draft.action_id, verbatim="yes",
                                       actor="PI", thread_id="t")
    second = executor.approve_and_apply(draft.action_id, verbatim="yes",
                                        actor="PI", thread_id="t")
    assert first.created_record_id == second.created_record_id
    assert second.idempotent_replay
    assert len(Sandbox(project_root).read(draft.target_ledger)) == 1


def test_a_stale_draft_is_refused(project_root):
    executor = ActionExecutor(project_root)
    routine = [d for d in drafts_for(project_root, "S-013")
               if not d.needs_exact_token]
    executor.save_drafts(routine[:2])

    executor.approve_and_apply(routine[0].action_id, verbatim="yes",
                               actor="PI", thread_id="t")
    outcome = executor.approve_and_apply(routine[1].action_id, verbatim="yes",
                                         actor="PI", thread_id="t")
    assert outcome.ok is False
    assert outcome.status == "stale"
    assert "re-run the review" in outcome.message


def test_rollback_compensates_and_never_deletes(project_root):
    executor = ActionExecutor(project_root)
    draft = next(d for d in drafts_for(project_root, "S-013")
                 if not d.needs_exact_token)
    executor.save_drafts([draft])
    applied = executor.approve_and_apply(draft.action_id, verbatim="yes",
                                         actor="PI", thread_id="t")

    outcome = executor.rollback(draft.action_id, verbatim="undo that",
                                actor="PI", thread_id="t")
    assert outcome.ok

    ledger = Sandbox(project_root).read(draft.target_ledger)
    assert len(ledger) == 1                     # the record is still there
    assert ledger[0]["reversed"] is True        # marked, not removed
    assert ledger[0][list(ledger[0])[0]] == applied.created_record_id

    events = [e["event_type"] for e in AuditTrail(project_root).events()]
    assert "ACTION_APPLIED" in events and "ACTION_ROLLED_BACK" in events


def test_rollback_is_idempotent(project_root):
    executor = ActionExecutor(project_root)
    draft = next(d for d in drafts_for(project_root, "S-013")
                 if not d.needs_exact_token)
    executor.save_drafts([draft])
    executor.approve_and_apply(draft.action_id, verbatim="yes", actor="PI", thread_id="t")
    executor.rollback(draft.action_id, verbatim="undo", actor="PI", thread_id="t")
    second = executor.rollback(draft.action_id, verbatim="undo", actor="PI", thread_id="t")
    assert second.idempotent_replay


def test_rejecting_writes_nothing_but_is_audited(project_root):
    executor = ActionExecutor(project_root)
    draft = drafts_for(project_root, "S-013")[0]
    executor.save_drafts([draft])

    outcome = executor.reject(draft.action_id, verbatim="no, the site already fixed this",
                              actor="PI", thread_id="t")
    assert outcome.ok and outcome.status == "rejected"
    assert Sandbox(project_root).read(draft.target_ledger) == []

    rejected = [e for e in AuditTrail(project_root).events()
                if e["event_type"] == "ACTION_REJECTED"]
    assert rejected[0]["approval_verbatim"] == "no, the site already fixed this"
