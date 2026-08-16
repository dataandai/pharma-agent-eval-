from __future__ import annotations

import json
import time
from pathlib import Path
from typing import List

from src.graph.builder import build_agent_graph, initial_state, run_turn


SAMPLE_PROMPTS: List[str] = [
    "review S-004",
    "review S-009",
    "review SITE-02",
    "review S-005",
    "what is an important deviation?",
    "which records could you not assess?",
    "what happened?",
]


def run_eval(root: Path | str, prompts: List[str] = SAMPLE_PROMPTS) -> dict:
    root = Path(root)
    graph = build_agent_graph(root)
    results = []
    for i, prompt in enumerate(prompts, start=1):
        tid = f"eval-{i}"
        state = initial_state(tid)
        t0 = time.perf_counter()
        new_state = run_turn(graph, state, prompt)
        took = time.perf_counter() - t0
        results.append({
            "thread_id": tid,
            "prompt": prompt,
            "response": new_state.get("response"),
            "messages": new_state.get("messages"),
            "time_s": took,
        })
    return {"results": results}


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    out = run_eval(root)
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
