from __future__ import annotations

import argparse
from pathlib import Path

from src.aws_agent_core_adapter import AWSAgentCoreAdapter


def main() -> None:
    p = argparse.ArgumentParser(description="Run the agent as an AWS-backed worker")
    p.add_argument("--queue-url", help="SQS queue URL to poll", required=False)
    p.add_argument("--root", help="Repository root path", default=str(Path(__file__).resolve().parents[1]))
    p.add_argument("--interval", help="Poll interval seconds", type=float, default=2.0)
    args = p.parse_args()

    adapter = AWSAgentCoreAdapter(args.root, queue_url=args.queue_url, poll_interval=args.interval)
    adapter.run()


if __name__ == "__main__":
    main()
