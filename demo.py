from pathlib import Path

from src.data.repositories import SandboxRepository
from src.graph.builder import build_agent_graph, initial_state, run_turn

ROOT = Path(__file__).resolve().parent
SandboxRepository(ROOT).reset()
graph = build_agent_graph(ROOT)
state = initial_state("cli-demo")

commands = [
    "investigate P-100",
    "What currency is that plan in?",
]

for command in commands:
    print(f"\nUSER: {command}")
    state = run_turn(graph, state, command)
    print(f"AGENT: {state['response']}")

if state.get("pending_actions"):
    action_id = state["pending_actions"][0]["action_id"]
    for command in [f"approve {action_id}", f"approve {action_id}", "What happened to P-100?"]:
        print(f"\nUSER: {command}")
        state = run_turn(graph, state, command)
        print(f"AGENT: {state['response']}")
