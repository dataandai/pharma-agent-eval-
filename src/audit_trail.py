"""Append-only, hash-chained audit trail.

More important here than in billing, not less: this is a regulated audit trail,
and an inspector reading it must be able to reconstruct who decided what, on
what evidence, and in what words.

Every approval event records five things the guideline cares about: the finding,
the proposed classification, the reasoning behind that proposal, who approved,
and the approval **verbatim**. Paraphrasing a human's approval into a boolean is
how accountability gets lost.

Rollback appends a compensating entry. It never deletes. History that can be
edited is not an audit trail.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.data.json_store import JsonStore


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _digest(row: dict[str, Any]) -> str:
    """The hash basis: every stored field except the hash itself."""
    basis = {k: v for k, v in row.items() if k != "event_hash"}
    return hashlib.sha256(
        json.dumps(basis, sort_keys=True, default=str,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class AuditEvent:
    event_id: str
    timestamp: str
    event_type: str
    thread_id: str
    summary: str
    schema_version: int = 1

    finding_id: str | None = None
    action_id: str | None = None
    action_type: str | None = None
    subject_id: str | None = None
    site_id: str | None = None
    visit_id: str | None = None

    proposed_classification: str | None = None
    classification_reasoning: str | None = None
    calculation: str | None = None

    approved_by: str | None = None
    approval_verbatim: str | None = None
    confirmation_level: str | None = None

    created_record_id: str | None = None
    sandbox_version_before: int | None = None
    sandbox_version_after: int | None = None

    previous_event_hash: str | None = None
    event_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AuditTrail:
    def __init__(self, root: Path):
        self.path = Path(root) / "sandbox" / "audit_log.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            JsonStore(self.path, []).write_atomic([])

    def _store(self) -> JsonStore:
        return JsonStore(self.path, [])

    def append(self, *, thread_id: str, event_type: str, summary: str,
               **fields: Any) -> AuditEvent:
        store = self._store()
        rows = store.read()
        previous = rows[-1]["event_hash"] if rows else None

        payload = {
            "event_id": f"EVT-{uuid4().hex[:12].upper()}",
            "timestamp": utc_now().isoformat(),
            "event_type": event_type,
            "thread_id": thread_id,
            "summary": summary,
            "previous_event_hash": previous,
            **fields,
        }
        # Hash the whole event as it will be stored, not the subset of fields
        # that happened to be passed in -- otherwise verification recomputes
        # over a different basis and every entry looks tampered with.
        event = AuditEvent(**payload)
        event = AuditEvent(**{**event.to_dict(), "event_hash": _digest(event.to_dict())})
        rows.append(event.to_dict())
        store.write_atomic(rows)
        return event

    def events(self) -> list[dict]:
        return self._store().read()

    def query(self, **filters) -> list[dict]:
        """Exact structured filter. No free-text search over an audit trail."""
        rows = self.events()
        for key, value in filters.items():
            if value is None:
                continue
            rows = [r for r in rows if r.get(key) == value]
        return rows

    def verify_chain(self) -> tuple[bool, str | None]:
        """Recompute the chain. A broken link means the log was edited."""
        previous = None
        for row in self.events():
            expected = _digest(row)
            if row.get("previous_event_hash") != previous:
                return False, f"{row['event_id']} does not chain to its predecessor"
            if row["event_hash"] != expected:
                return False, f"{row['event_id']} hash does not match its content"
            previous = row["event_hash"]
        return True, None
