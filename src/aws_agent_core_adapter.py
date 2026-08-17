from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import boto3

from src.graph.builder import build_agent_graph, initial_state, run_turn

logger = logging.getLogger(__name__)


class AWSAgentCoreAdapter:
    """Lightweight adapter that polls an SQS queue for user messages and
    invokes the agent graph. This is a small, non-opinionated scaffold so the
    project can be run on an AWS-hosted agent core (or locally with a queue).

    Messages expected on the queue are JSON objects with at least a
    `message` string and an optional `thread_id`. Example:
      {"thread_id": "aws-1", "message": "review S-004"}
    """

    def __init__(self, root: str | Path, queue_url: str | None = None,
                 poll_interval: float = 2.0):
        self.root = Path(root)
        self.queue_url = queue_url or os.getenv("AWS_SQS_QUEUE_URL")
        if not self.queue_url:
            raise ValueError("AWS_SQS_QUEUE_URL must be set or queue_url provided")
        self.poll_interval = float(poll_interval)
        self.sqs = boto3.client("sqs")
        self.graph = build_agent_graph(self.root)

    def _handle_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        thread_id = payload.get("thread_id") or "aws-thread"
        message = payload.get("message", "")
        state = initial_state(thread_id)
        return run_turn(self.graph, state, message)

    def poll_once(self) -> None:
        resp = self.sqs.receive_message(
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=10,
            MessageAttributeNames=["All"],
        )
        for msg in resp.get("Messages", []):
            body = msg.get("Body", "")
            try:
                payload = json.loads(body) if body else {"message": ""}
            except Exception:
                payload = {"message": body}

            # Extract thread_id for logging and scope availability
            thread_id = payload.get("thread_id") or "aws-thread"
            handled_successfully = False

            try:
                result = self._handle_message(payload)
                handled_successfully = True
                # Simple visibility: log to stdout. Deployments can replace this
                # with SNS publishes, another queue, or storing results in S3.
                print(json.dumps({
                    "thread_id": result.get("thread_id", thread_id),
                    "response": result.get("response"),
                }))
            except Exception:
                logger.exception("handling queue message failed for thread %s", thread_id)

            # Only delete from queue if processing succeeded; on error, message
            # remains visible for retry (configure AWS SQS visibility timeout
            # and max receive count for dead-letter queue routing)
            if handled_successfully:
                try:
                    self.sqs.delete_message(
                        QueueUrl=self.queue_url,
                        ReceiptHandle=msg.get("ReceiptHandle"),
                    )
                except Exception:
                    logger.exception("failed to delete SQS message for thread %s", thread_id)

    def run(self) -> None:
        logger.info("starting AWSAgentCoreAdapter; polling %s", self.queue_url)
        while True:
            try:
                self.poll_once()
            except Exception:
                logger.exception("polling iteration failed")
            time.sleep(self.poll_interval)
