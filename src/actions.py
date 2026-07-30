"""Corrective actions: drafted from findings, written only through the gate.

Five actions, richer than the billing set because E6(R3) requires prevention as
well as documentation:

| action                      | when                                      |
|-----------------------------|-------------------------------------------|
| log_deviation               | the default record, with a proposed class |
| raise_site_query            | data is missing or needs correction       |
| open_capa                   | systemic or recurring -- the prevention   |
| propose_protocol_amendment  | the protocol is the problem, not the site |
| escalate_to_medical_monitor | safety-relevant, time-bound               |

**Graduated confirmation.** A routine out-of-window entry takes a plain
affirmative. Classifying something as an *important deviation*, or escalating to
the medical monitor, requires the exact token. That is not ceremony: those two
have downstream regulatory effect -- an important deviation reaches the IRB/EC,
and an escalation starts a clock.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from src.audit_trail import AuditTrail, utc_now
from src.findings import Family, Finding, ProposedAction
from src.sandbox import LEDGER_FOR_ACTION, Sandbox

PLAIN_AFFIRMATIVES = {
    "yes", "y", "ok", "okay", "approve", "approved", "confirm", "confirmed",
    "go ahead", "do it", "jóváhagyom", "jóváhagy", "igen", "rendben",
}

TOKEN_TEMPLATE = "APPROVE {action_id}"


class ConfirmationLevel:
    PLAIN = "plain"
    TOKEN = "token"


@dataclass(frozen=True)
class ActionDraft:
    action_id: str
    action_type: str
    finding_id: str
    detector: str
    subject_id: str | None
    site_id: str | None
    visit_id: str | None
    governing_version: str | None
    calculation: str
    proposed_classification: str | None
    classification_reasoning: str | None
    evidence: tuple[str, ...]
    target_ledger: str
    payload: dict[str, Any]
    confirmation_level: str
    required_token: str | None
    sandbox_version: int
    proposal_hash: str
    created_at: str
    status: str = "proposed"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["evidence"] = list(self.evidence)
        return data

    @property
    def needs_exact_token(self) -> bool:
        return self.confirmation_level == ConfirmationLevel.TOKEN


def canonical_payload(values: dict) -> str:
    return json.dumps(values, sort_keys=True, separators=(",", ":"), default=str)


def proposal_hash(values: dict) -> str:
    return hashlib.sha256(canonical_payload(values).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Drafting
# --------------------------------------------------------------------------

def _confirmation_for(action_type: str, finding: Finding) -> str:
    """Two things need the exact token, and only two."""
    if action_type == ProposedAction.ESCALATE_TO_MEDICAL_MONITOR.value:
        return ConfirmationLevel.TOKEN
    if (action_type == ProposedAction.LOG_DEVIATION.value
            and finding.classification
            and finding.classification.proposed == "important"):
        return ConfirmationLevel.TOKEN
    return ConfirmationLevel.PLAIN


def _payload_for(action_type: str, finding: Finding, governing_version: str | None) -> dict:
    common = {
        "finding_id": finding.finding_id,
        "detector": finding.detector,
        "subject_id": finding.subject_id,
        "site_id": finding.site_id,
        "visit_id": finding.visit_id,
        "record_ids": list(finding.record_ids),
        "calculation": finding.calculation,
        "raised_on": date.today().isoformat(),
    }
    if action_type == ProposedAction.LOG_DEVIATION.value:
        return {
            **common,
            "protocol_version_governing": governing_version,
            "proposed_classification": finding.classification.proposed
            if finding.classification else None,
            "classification_reasoning": finding.classification.reasoning
            if finding.classification else None,
            "classification_status": "proposed_pending_investigator_review",
            "guideline_reference": finding.classification.guideline_reference
            if finding.classification else None,
        }
    if action_type == ProposedAction.RAISE_SITE_QUERY.value:
        return {**common, "question": _query_text(finding), "status": "open"}
    if action_type == ProposedAction.OPEN_CAPA.value:
        return {**common, "problem_statement": finding.calculation,
                "prevention_required": True, "status": "open"}
    if action_type == ProposedAction.PROPOSE_PROTOCOL_AMENDMENT.value:
        return {**common, "current_version": governing_version,
                "rationale": finding.calculation, "status": "proposed"}
    if action_type == ProposedAction.ESCALATE_TO_MEDICAL_MONITOR.value:
        return {**common, "urgency": "time_bound", "status": "open",
                "evidence": list(finding.evidence)}
    return common


def _query_text(finding: Finding) -> str:
    return (f"Please review and correct or confirm the source data for "
            f"{finding.subject_id or 'this record'}"
            f"{f' {finding.visit_id}' if finding.visit_id else ''}. "
            f"{finding.calculation}")


def draft_actions(findings: list[Finding], sandbox: Sandbox,
                  governing_versions: dict[str, str] | None = None) -> list[ActionDraft]:
    """One draft per proposed action on every reportable finding.

    A suppressed finding drafts nothing: that is what suppression means.
    """
    versions = governing_versions or {}
    drafts: list[ActionDraft] = []
    version_at = sandbox.version()

    for finding in findings:
        if not finding.is_reportable:
            continue
        governing = versions.get(finding.subject_id or "")
        for action in finding.proposed_actions:
            action_id = f"ACT-{uuid4().hex[:10].upper()}"
            ledger, _id_field, _prefix = LEDGER_FOR_ACTION[action.value]
            payload = _payload_for(action.value, finding, governing)
            hashed = proposal_hash({
                "action_type": action.value, "finding_id": finding.finding_id,
                "payload": payload,
            })
            drafts.append(ActionDraft(
                action_id=action_id,
                action_type=action.value,
                finding_id=finding.finding_id,
                detector=finding.detector,
                subject_id=finding.subject_id,
                site_id=finding.site_id,
                visit_id=finding.visit_id,
                governing_version=governing,
                calculation=finding.calculation,
                proposed_classification=finding.classification.proposed
                if finding.classification else None,
                classification_reasoning=finding.classification.reasoning
                if finding.classification else None,
                evidence=finding.evidence,
                target_ledger=ledger,
                payload=payload,
                confirmation_level=_confirmation_for(action.value, finding),
                required_token=TOKEN_TEMPLATE.format(action_id=action_id)
                if _confirmation_for(action.value, finding) == ConfirmationLevel.TOKEN
                else None,
                sandbox_version=version_at,
                proposal_hash=hashed,
                created_at=utc_now().isoformat(),
            ))
    return drafts


# --------------------------------------------------------------------------
# Approval
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ApprovalOutcome:
    ok: bool
    status: str
    message: str
    action_id: str
    created_record_id: str | None = None
    sandbox_version: int | None = None
    idempotent_replay: bool = False


def check_confirmation(draft: ActionDraft, text: str) -> tuple[bool, str]:
    """Is this reply a valid approval for this draft?

    Returns (accepted, reason). A plain affirmative never satisfies a draft that
    requires the exact token -- that is the entire point of the graduation.
    """
    cleaned = (text or "").strip()
    if draft.needs_exact_token:
        if draft.required_token and draft.required_token in cleaned:
            return True, "exact token supplied"
        return False, (
            f"This action requires the exact confirmation token because "
            f"{'it escalates to the medical monitor' if draft.action_type.startswith('escalate') else 'it proposes an important deviation'}, "
            f"which has downstream regulatory effect. Reply with exactly: "
            f"{draft.required_token}"
        )
    if cleaned.lower() in PLAIN_AFFIRMATIVES:
        return True, "plain affirmative"
    return False, "no clear affirmative; nothing was written"


class ActionExecutor:
    """The only thing in the system that writes."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.sandbox = Sandbox(self.root)
        self.audit = AuditTrail(self.root)

    def save_drafts(self, drafts: list[ActionDraft]) -> None:
        for draft in drafts:
            self.sandbox.append_unique("proposals", draft.to_dict(), "action_id")

    def get_draft(self, action_id: str) -> ActionDraft | None:
        raw = self.sandbox.find("proposals", "action_id", action_id)
        if raw is None:
            return None
        raw = dict(raw)
        raw["evidence"] = tuple(raw.get("evidence", ()))
        return ActionDraft(**raw)

    def reject(self, action_id: str, *, verbatim: str, actor: str,
               thread_id: str) -> ApprovalOutcome:
        draft = self.get_draft(action_id)
        if draft is None:
            return ApprovalOutcome(False, "unknown", f"Unknown action {action_id}.", action_id)
        self.sandbox.update_where("proposals", "action_id", action_id, status="rejected")
        self.audit.append(
            thread_id=thread_id, event_type="ACTION_REJECTED",
            summary=f"{action_id} ({draft.action_type}) was rejected; nothing was written.",
            action_id=action_id, action_type=draft.action_type,
            finding_id=draft.finding_id, subject_id=draft.subject_id,
            site_id=draft.site_id, visit_id=draft.visit_id,
            approved_by=actor, approval_verbatim=verbatim,
        )
        return ApprovalOutcome(True, "rejected",
                               "Rejected. No record was written.", action_id)

    def approve_and_apply(self, action_id: str, *, verbatim: str, actor: str,
                          thread_id: str) -> ApprovalOutcome:
        draft = self.get_draft(action_id)
        if draft is None:
            return ApprovalOutcome(False, "unknown", f"Unknown action {action_id}.", action_id)

        accepted, reason = check_confirmation(draft, verbatim)
        if not accepted:
            self.audit.append(
                thread_id=thread_id, event_type="APPROVAL_REFUSED",
                summary=f"Approval for {action_id} was not accepted: {reason}",
                action_id=action_id, action_type=draft.action_type,
                finding_id=draft.finding_id, approved_by=actor,
                approval_verbatim=verbatim,
                confirmation_level=draft.confirmation_level,
            )
            return ApprovalOutcome(False, "not_confirmed", reason, action_id)

        existing = self.sandbox.find("applied_actions", "action_id", action_id)
        if existing:
            return ApprovalOutcome(
                True, "applied", "Already applied; the original result was returned.",
                action_id, existing.get("created_record_id"),
                existing.get("sandbox_version_after"), idempotent_replay=True,
            )

        current = self.sandbox.version()
        if current != draft.sandbox_version:
            self.sandbox.update_where("proposals", "action_id", action_id, status="stale")
            return ApprovalOutcome(
                False, "stale",
                f"The sandbox changed since this action was drafted (version "
                f"{draft.sandbox_version} -> {current}). It was not applied; re-run the "
                f"review so the approval refers to the world you were shown.",
                action_id,
            )

        ledger, id_field, prefix = LEDGER_FOR_ACTION[draft.action_type]
        record_id = f"{prefix}-{uuid4().hex[:8].upper()}"
        record = {
            id_field: record_id,
            "action_id": action_id,
            "approved_by": actor,
            "approval_verbatim": verbatim,
            "approved_at": utc_now().isoformat(),
            "reversed": False,
            **draft.payload,
        }
        self.sandbox.append_unique(ledger, record, id_field)
        before, after = self.sandbox.bump_version()

        self.sandbox.append_unique("applied_actions", {
            "action_id": action_id, "action_type": draft.action_type,
            "ledger": ledger, "id_field": id_field, "created_record_id": record_id,
            "status": "applied", "sandbox_version_before": before,
            "sandbox_version_after": after,
        }, "action_id")
        self.sandbox.update_where("proposals", "action_id", action_id, status="applied")

        self.audit.append(
            thread_id=thread_id, event_type="ACTION_APPLIED",
            summary=f"Applied {draft.action_type} as {record_id}.",
            action_id=action_id, action_type=draft.action_type,
            finding_id=draft.finding_id, subject_id=draft.subject_id,
            site_id=draft.site_id, visit_id=draft.visit_id,
            proposed_classification=draft.proposed_classification,
            classification_reasoning=draft.classification_reasoning,
            calculation=draft.calculation,
            approved_by=actor, approval_verbatim=verbatim,
            confirmation_level=draft.confirmation_level,
            created_record_id=record_id,
            sandbox_version_before=before, sandbox_version_after=after,
        )
        return ApprovalOutcome(True, "applied",
                               f"Written to {ledger} as {record_id}.",
                               action_id, record_id, after)

    def rollback(self, action_id: str, *, verbatim: str, actor: str,
                 thread_id: str) -> ApprovalOutcome:
        """Appends a compensating entry. Never deletes."""
        applied = self.sandbox.find("applied_actions", "action_id", action_id)
        if applied is None:
            return ApprovalOutcome(False, "unknown",
                                   f"{action_id} was never applied.", action_id)
        if applied["status"] == "rolled_back":
            return ApprovalOutcome(True, "rolled_back",
                                   "Already rolled back.", action_id,
                                   idempotent_replay=True)

        rollback_id = f"RB-{uuid4().hex[:10].upper()}"
        self.sandbox.update_where(applied["ledger"], applied["id_field"],
                                  applied["created_record_id"],
                                  reversed=True, reversed_by=rollback_id)
        before, after = self.sandbox.bump_version()
        self.sandbox.append_unique("rollbacks", {
            "rollback_id": rollback_id, "action_id": action_id,
            "reversed_record_id": applied["created_record_id"],
            "ledger": applied["ledger"], "reversed_by": actor,
            "reversal_verbatim": verbatim,
            "sandbox_version_before": before, "sandbox_version_after": after,
        }, "rollback_id")
        self.sandbox.update_where("applied_actions", "action_id", action_id,
                                  status="rolled_back", rollback_id=rollback_id)

        self.audit.append(
            thread_id=thread_id, event_type="ACTION_ROLLED_BACK",
            summary=(f"Rolled back {action_id} with compensating entry {rollback_id}. "
                     f"The original record {applied['created_record_id']} is marked "
                     f"reversed and remains in the ledger."),
            action_id=action_id, action_type=applied["action_type"],
            created_record_id=applied["created_record_id"],
            approved_by=actor, approval_verbatim=verbatim,
            sandbox_version_before=before, sandbox_version_after=after,
        )
        return ApprovalOutcome(True, "rolled_back",
                               f"Rolled back using compensating entry {rollback_id}. "
                               f"Nothing was deleted.", action_id, rollback_id, after)
