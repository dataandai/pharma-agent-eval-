from __future__ import annotations

from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    thread_id: str
    messages: list[dict[str, str]]
    user_message: str
    intent: str
    _decision: dict[str, Any]
    active_plan_id: str | None
    plan: dict[str, Any] | None
    reconciliation_rows: list[dict[str, Any]]
    findings: list[dict[str, Any]]
    pending_actions: list[dict[str, Any]]
    applied_actions: list[str]
    response: str
    error: str | None
