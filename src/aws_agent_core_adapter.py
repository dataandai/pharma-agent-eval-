from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import boto3

from src.graph.builder import (
    build_agent_graph,
    initial_state,
    run_turn,
    resume_turn,
    pending_interrupt,
)
from src.graph.checkpoint import FileSystemCheckpointStore
from src.interpreter import Intent

logger = logging.getLogger(__name__)


class AWSAgentCoreAdapter:
    """Lightweight adapter that polls an SQS queue for user messages and
    invokes the agent graph. Supports HITL (human-in-the-loop) approval gates.

    Messages expected on the queue are JSON objects with at least a `message`
    string and an optional `thread_id`. Examples:
      {"thread_id": "aws-1", "message": "review S-004"}
      {"thread_id": "aws-1", "message": "approve ACT-XXXX"}

    Session state is kept in memory (in-process) for development/testing.
    For production, replace self.sessions with a persistent store (DynamoDB, S3, etc.)
    and the methods that interact with it.
    """

    def __init__(self, root: str | Path, queue_url: str | None = None,
                 poll_interval: float = 2.0, checkpoint_store = None):
        self.root = Path(root)
        self.queue_url = queue_url or os.getenv("AWS_SQS_QUEUE_URL")
        if not self.queue_url:
            raise ValueError("AWS_SQS_QUEUE_URL must be set or queue_url provided")
        self.poll_interval = float(poll_interval)
        self.sqs = boto3.client("sqs")
        self.graph = build_agent_graph(self.root)
        # Use provided checkpoint store or default to filesystem-based store
        # in the sandbox/checkpoints directory
        if checkpoint_store is None:
            checkpoint_dir = self.root / "sandbox" / "checkpoints"
            checkpoint_store = FileSystemCheckpointStore(checkpoint_dir)
        self.checkpoint_store = checkpoint_store
        # In-memory cache for current session (speeds up multi-message threads)
        # Backed by persistent checkpoint store
        self.sessions: dict[str, dict] = {}

    def _get_session(self, thread_id: str) -> dict | None:
        """Retrieve a saved session state from in-memory cache or persistent store."""
        # First check in-memory cache
        if thread_id in self.sessions:
            return self.sessions[thread_id]
        # Fall back to persistent checkpoint store
        config = {"configurable": {"thread_id": thread_id}}
        state = self.checkpoint_store.get(config)
        if state:
            self.sessions[thread_id] = state
        return state

    def _save_session(self, thread_id: str, state: dict) -> None:
        """Save session state to in-memory cache and persistent store."""
        self.sessions[thread_id] = state
        config = {"configurable": {"thread_id": thread_id}}
        self.checkpoint_store.put(config, state, metadata={"timestamp": __import__("time").time()})

    def _handle_message(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Process one SQS message: either start a new turn or resume a paused gate."""
        thread_id = payload.get("thread_id") or "aws-thread"
        message = payload.get("message", "").strip()

        # Check if this looks like an approval (APPROVE ACT-XXX or just "yes"/"ok")
        # or a rejection/rollback — if so, try to resume from a paused gate.
        is_approval = any(
            x in message.lower() for x in ("approve", "yes", "ok", "confirm")
        )
        is_rejection = any(
            x in message.lower() for x in ("reject", "decline", "no", "rollback")
        )

        if (is_approval or is_rejection) and thread_id in self.sessions:
            # Try to resume a paused gate
            previous_state = self._get_session(thread_id)
            card = pending_interrupt(self.graph, previous_state)
            if card is not None:
                # Gate is paused; resume with this reply
                logger.info("Resuming gate for thread %s with message: %s", thread_id, message)
                new_state = resume_turn(self.graph, previous_state, message)
                self._save_session(thread_id, new_state)
                return new_state

        # Otherwise, treat as a fresh query or message
        state = self._get_session(thread_id) or initial_state(thread_id)
        logger.info("Running turn for thread %s with message: %s", thread_id, message)
        new_state = run_turn(self.graph, state, message)
        self._save_session(thread_id, new_state)
        return new_state

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
