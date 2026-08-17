"""Persistent checkpoint store using JSON files (development/testing).

This module provides a simple, file-based checkpoint store for agent state.
It is suitable for development and testing but has limitations:
  - Not thread-safe or process-safe
  - No concurrent access support
  - No compression

For production, replace with DynamoDB, PostgreSQL, Redis, etc.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class FileSystemCheckpointStore:
    """Stores agent checkpoints as JSON files in a directory.
    
    Each checkpoint is stored as checkpoint/{thread_id}/{checkpoint_id}.json.
    """

    def __init__(self, checkpoint_dir: Path | str):
        self.checkpoint_dir = Path(checkpoint_dir)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)

    def put(self, config: dict, values: dict, metadata: dict | None = None) -> str:
        """Store a checkpoint. Returns the checkpoint ID (v1 by default)."""
        thread_id = config.get("configurable", {}).get("thread_id", "default")
        thread_dir = self.checkpoint_dir / thread_id
        thread_dir.mkdir(parents=True, exist_ok=True)
        
        # Use a simple incrementing ID or timestamp-based ID
        import time
        checkpoint_id = str(int(time.time() * 1000))  # millisecond timestamp
        
        checkpoint_path = thread_dir / f"{checkpoint_id}.json"
        checkpoint_path.write_text(json.dumps({
            "config": config,
            "values": values,
            "metadata": metadata or {},
        }, default=str))
        
        return checkpoint_id

    def get(self, config: dict) -> dict | None:
        """Retrieve the latest checkpoint for a thread."""
        thread_id = config.get("configurable", {}).get("thread_id", "default")
        thread_dir = self.checkpoint_dir / thread_id
        
        if not thread_dir.exists():
            return None
        
        # Get the latest checkpoint (highest timestamp)
        checkpoints = sorted(thread_dir.glob("*.json"))
        if not checkpoints:
            return None
        
        latest = checkpoints[-1]
        data = json.loads(latest.read_text())
        return data.get("values")

    def get_tuple(self, config: dict) -> tuple[dict, dict] | None:
        """Retrieve the latest checkpoint and its metadata."""
        thread_id = config.get("configurable", {}).get("thread_id", "default")
        thread_dir = self.checkpoint_dir / thread_id
        
        if not thread_dir.exists():
            return None
        
        checkpoints = sorted(thread_dir.glob("*.json"))
        if not checkpoints:
            return None
        
        latest = checkpoints[-1]
        data = json.loads(latest.read_text())
        return data.get("values"), data.get("metadata", {})
