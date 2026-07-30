from __future__ import annotations

from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    """Every field earns its place.

    `active_subject_id` is what makes "why was that one a deviation?" work
    without repeating the identifier. `pending_actions` is what the gate
    approves against -- an approval refers to a stored draft, never to whatever
    the model remembers. `applied_actions` is what makes rollback possible.
    """

    thread_id: str
    actor: str
    messages: list[dict[str, str]]
    user_message: str

    intent: str
    _decision: dict[str, Any]

    active_subject_id: str | None
    active_site_id: str | None
    findings: list[dict[str, Any]]
    pending_actions: list[dict[str, Any]]
    applied_actions: list[str]

    # Set by the gate, read by the single write node.
    gate_action_id: str | None
    gate_decision: str | None       # approve | reject | rollback
    gate_passed: bool
    gate_verbatim: str | None

    response: str
    error: str | None
