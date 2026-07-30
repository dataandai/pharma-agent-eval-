"""The writable sandbox: five ledgers, a version counter, atomic writes.

Nothing here is reachable except through the approval gate. The version counter
is what makes a proposal go stale: a draft approved against a sandbox that has
since changed underneath it is refused rather than applied to a different world
than the one the human reviewed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.data.json_store import JsonStore

LEDGERS: dict[str, Any] = {
    # The five corrective actions, one ledger each.
    "deviation_entries": [],
    "site_queries": [],
    "capas": [],
    "amendment_proposals": [],
    "escalations": [],
    # Lifecycle.
    "proposals": [],
    "applied_actions": [],
    "rollbacks": [],
    "audit_log": [],
    "metadata": {"sandbox_version": 0},
}

LEDGER_FOR_ACTION = {
    "log_deviation": ("deviation_entries", "deviation_entry_id", "DEV-S"),
    "raise_site_query": ("site_queries", "query_id", "QRY"),
    "open_capa": ("capas", "capa_id", "CAPA"),
    "propose_protocol_amendment": ("amendment_proposals", "amendment_id", "AMD"),
    "escalate_to_medical_monitor": ("escalations", "escalation_id", "ESC"),
}


class Sandbox:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.dir = self.root / "sandbox"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ensure_files()

    def _store(self, name: str) -> JsonStore:
        return JsonStore(self.dir / f"{name}.json", LEDGERS[name])

    def ensure_files(self) -> None:
        for name, default in LEDGERS.items():
            path = self.dir / f"{name}.json"
            if not path.exists():
                JsonStore(path, default).write_atomic(
                    dict(default) if isinstance(default, dict) else list(default)
                )

    def reset(self) -> None:
        for name, default in LEDGERS.items():
            self._store(name).write_atomic(
                dict(default) if isinstance(default, dict) else list(default)
            )

    # -- version ----------------------------------------------------------
    def version(self) -> int:
        return int(self._store("metadata").read()["sandbox_version"])

    def bump_version(self) -> tuple[int, int]:
        store = self._store("metadata")
        metadata = store.read()
        before = int(metadata["sandbox_version"])
        metadata["sandbox_version"] = before + 1
        store.write_atomic(metadata)
        return before, before + 1

    # -- ledgers ----------------------------------------------------------
    def read(self, name: str) -> list[dict]:
        return self._store(name).read()

    def append_unique(self, name: str, record: dict, key: str) -> tuple[dict, bool]:
        """Returns (record, was_new). Appending the same key twice is a no-op,
        which is what makes execution idempotent."""
        store = self._store(name)
        rows = store.read()
        for existing in rows:
            if existing.get(key) == record.get(key):
                return existing, False
        rows.append(record)
        store.write_atomic(rows)
        return record, True

    def update_where(self, name: str, key: str, value: str, **changes) -> dict | None:
        store = self._store(name)
        rows = store.read()
        for row in rows:
            if row.get(key) == value:
                row.update(changes)
                store.write_atomic(rows)
                return row
        return None

    def find(self, name: str, key: str, value: str) -> dict | None:
        return next((r for r in self.read(name) if r.get(key) == value), None)
