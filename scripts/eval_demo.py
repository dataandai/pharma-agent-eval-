from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Ensure project root is importable as a top-level package path when invoked
# from scripts/ using a plain `python scripts/...` call.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.graph.builder import (
    build_agent_graph,
    initial_state,
    pending_interrupt,
    resume_turn,
    run_turn,
)
from src.sandbox import Sandbox
from src.audit_trail import AuditTrail


def _resume(graph, state, reply: str):
    """Resume a paused gate; use the library helper when available,
    otherwise fall back to invoking the compiled graph with the user reply.
    """
    try:
        from src.graph.builder import resume_turn as _builder_resume  # type: ignore
        return _builder_resume(graph, state, reply)
    except Exception:
        # Fallback for the lightweight compiled graph
        next_state = dict(state)
        next_state["user_message"] = reply
        result = graph.invoke(next_state, config={"configurable": {"thread_id": state["thread_id"]}})
        merged = dict(state)
        merged.update(result or {})
        return merged


# ROOT is set above and also used by helper classes


def run_demo() -> dict[str, Any]:
    Sandbox(ROOT).reset()
    graph = build_agent_graph(ROOT)
    state = initial_state("eval-demo")

    prompts = [
        "review S-013",
        "review S-004",
        "review S-009",
        "review SITE-02",
        "review S-005",
    ]

    steps: list[dict[str, Any]] = []

    for prompt in prompts:
        entry: dict[str, Any] = {"prompt": prompt}
        state = run_turn(graph, state, prompt)
        entry["response"] = state.get("response")
        try:
            card = pending_interrupt(graph, state)
        except Exception:
            # Fallback for environments without langgraph: synthesize a minimal
            # approval card from the first proposed pending action in state.
            pending_local = [d for d in state.get("pending_actions", []) if d.get("status") == "proposed"]
            if pending_local:
                d = pending_local[0]
                card = {
                    "action_id": d.get("action_id"),
                    "action": d.get("action_type"),
                    "subject": d.get("subject_id"),
                    "visit": d.get("visit_id"),
                    "protocol_version_governing_subject": d.get("governing_version"),
                    "will_write_to": f"sandbox/{d.get('target_ledger')}.json",
                    "proposed_classification": d.get("proposed_classification"),
                    "confirmation_required": d.get("confirmation_level"),
                    "required_token": d.get("required_token"),
                    "evidence": d.get("evidence", []),
                }
            else:
                card = None
        if card:
            entry["paused_at_gate"] = True
            entry["card"] = card
            # Find the pending action in the state and simulate approval flow
            pending = [d for d in state.get("pending_actions", []) if d["status"] == "proposed"]
            entry["pending_actions"] = pending
            # If any requires a token, demonstrate the refusal-to-accept-plain-yes
            for action in pending:
                aid = action["action_id"]
                # try approve by naming the action
                state = run_turn(graph, state, f"approve {aid}")
                # try a plain 'yes' (expected to be refused if token required)
                state = _resume(graph, state, "yes")
                # if token required, send the exact token
                if action.get("needs_exact_token") or action.get("confirmation_level") == "token":
                    token = action.get("required_token")
                    state = _resume(graph, state, token)
                else:
                    # accept with a plain 'yes' if not token-locked
                    state = _resume(graph, state, "yes")
            entry["post_gate_response"] = state.get("response")
        else:
            entry["paused_at_gate"] = False
        steps.append(entry)

    # collect audit trail
    audit = AuditTrail(ROOT)
    events = audit.events()

    result = {"steps": steps, "audit_events": events}
    return result


def main() -> None:
    out = run_demo()
    out_path = Path(__file__).resolve().parents[0] / "eval_demo_output.json"
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"Wrote demo output to {out_path}")


if __name__ == "__main__":
    main()
