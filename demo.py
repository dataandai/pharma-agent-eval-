"""Offline CLI walkthrough of the four things worth showing.

Runs the same graph the Streamlit app does, with the rule-based interpreter,
so it needs no API key and no network.
"""

import sys
from pathlib import Path

# The findings use en and em dashes. A Windows console defaults to a codepage
# that cannot encode them, which turns the whole walkthrough into mojibake --
# and this is the first command anyone runs.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.graph.builder import (  # noqa: E402
    build_agent_graph,
    initial_state,
    pending_interrupt,
    resume_turn,
    run_turn,
)
from src.sandbox import Sandbox

ROOT = Path(__file__).resolve().parent
Sandbox(ROOT).reset()

graph = build_agent_graph(ROOT)
state = initial_state("cli-demo")


def say(message: str) -> None:
    global state
    print(f"\n{'=' * 76}\nUSER: {message}\n{'-' * 76}")
    state = run_turn(graph, state, message)
    card = pending_interrupt(graph, state)
    if card:
        print("AGENT: [paused at the approval gate]\n")
        print(f"  action   {card['action_id']}  {card['action']}")
        print(f"  subject  {card['subject']}   visit {card['visit']}")
        print(f"  governed by protocol {card['protocol_version_governing_subject']}")
        print(f"  writes to {card['will_write_to']}")
        if card["proposed_classification"]:
            print(f"  proposed classification: {card['proposed_classification']}")
        if card["confirmation_required"] == "token":
            print(f"  REQUIRES EXACT TOKEN: {card['required_token']}")
    else:
        print(f"AGENT: {state['response']}")


def reply(text: str) -> None:
    global state
    print(f"\nUSER (at the gate): {text!r}")
    state = resume_turn(graph, state, text)
    print(f"AGENT: {state['response']}")


# 1. A routine finding.
say("review S-013")

# 2. The version pair: same lateness, opposite verdict.
say("review S-004")
say("review S-009")

# 3. The systemic pattern: one amendment, not four deviation reports.
say("review SITE-02")

# 4. The closer: the immediate-hazard case.
say("review S-005")

# The gate refuses a plain affirmative on an important deviation.
pending = [d for d in state.get("pending_actions", [])
           if d["confirmation_level"] == "token"]
if pending:
    say(f"approve {pending[0]['action_id']}")
    reply("yes")
    reply(pending[0]["required_token"])

say("what happened?")
