"""Findings, and the classification the agent proposes but never decides.

ICH E6(R3) places the review of protocol deviations on the investigator -- a
named, accountable human. So every classification here is a *proposal* carrying
its reasoning, and it says so in its own data. A system that writes
classifications unattended is not merely risky, it is misaligned with the
guideline it claims to serve.

Two families of finding, deliberately kept apart:

- **protocol_deviation** -- about the subject and the protocol. Goes to the
  deviation log and, depending on classification, to the IRB/EC.
- **data_quality** -- about the site's records. Goes to a query or a CAPA.

Conflating them is a real-world mistake. Filing a data entry problem as a
deviation inflates the deviation rate the sponsor reports to the regulator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Iterable

from src.verdicts import Verdict


class Family(StrEnum):
    PROTOCOL_DEVIATION = "protocol_deviation"
    DATA_QUALITY = "data_quality"


class ProposedAction(StrEnum):
    LOG_DEVIATION = "log_deviation"
    RAISE_SITE_QUERY = "raise_site_query"
    OPEN_CAPA = "open_capa"
    PROPOSE_PROTOCOL_AMENDMENT = "propose_protocol_amendment"
    ESCALATE_TO_MEDICAL_MONITOR = "escalate_to_medical_monitor"


# ICH E6(R3): an important deviation is one that might significantly affect the
# completeness, accuracy or reliability of the study data, or the participant's
# rights, safety or well-being.
IMPORTANT = "important"
NOT_IMPORTANT = "not important"


@dataclass(frozen=True)
class ClassificationProposal:
    """A proposal, never a decision.

    `major` / `minor` / `critical` are sponsor house terminology, not regulatory
    categories, and are deliberately not used here.
    """

    proposed: str
    reasoning: str
    guideline_reference: str = "ICH E6(R3) Glossary, important protocol deviation"
    is_proposal: bool = True
    decided_by: str | None = None

    def __post_init__(self):
        if self.proposed not in (IMPORTANT, NOT_IMPORTANT):
            raise ValueError(f"classification must be {IMPORTANT!r} or {NOT_IMPORTANT!r}")

    @property
    def requires_investigator_review(self) -> bool:
        return self.decided_by is None

    def to_dict(self) -> dict:
        return {
            "proposed": self.proposed,
            "reasoning": self.reasoning,
            "guideline_reference": self.guideline_reference,
            "is_proposal": self.is_proposal,
            "decided_by": self.decided_by,
        }


@dataclass(frozen=True)
class Finding:
    detector: str
    family: Family
    verdict: Verdict
    calculation: str
    subject_id: str | None = None
    site_id: str | None = None
    visit_id: str | None = None
    record_ids: tuple[str, ...] = ()
    classification: ClassificationProposal | None = None
    proposed_actions: tuple[ProposedAction, ...] = ()
    evidence: tuple[str, ...] = ()
    suppressed_by: str | None = None
    subsumes: tuple[str, ...] = ()
    finding_id: str = ""

    @property
    def is_suppressed(self) -> bool:
        return self.suppressed_by is not None

    @property
    def is_reportable(self) -> bool:
        """A suppressed finding still exists and is still auditable; it just
        does not get filed on its own."""
        return not self.is_suppressed

    def with_id(self, finding_id: str) -> "Finding":
        return replace_finding(self, finding_id=finding_id)

    def suppressed(self, by: str) -> "Finding":
        return replace_finding(self, suppressed_by=by)

    def to_dict(self) -> dict:
        return {
            "finding_id": self.finding_id,
            "detector": self.detector,
            "family": self.family.value,
            "verdict": self.verdict.value,
            "subject_id": self.subject_id,
            "site_id": self.site_id,
            "visit_id": self.visit_id,
            "record_ids": list(self.record_ids),
            "calculation": self.calculation,
            "classification": self.classification.to_dict() if self.classification else None,
            "proposed_actions": [a.value for a in self.proposed_actions],
            "evidence": list(self.evidence),
            "suppressed_by": self.suppressed_by,
            "subsumes": list(self.subsumes),
        }


def replace_finding(finding: Finding, **changes) -> Finding:
    from dataclasses import replace
    return replace(finding, **changes)


def assign_ids(findings: Iterable[Finding], start: int = 1) -> list[Finding]:
    """Stable, deterministic IDs. Ordering must not depend on dict iteration or
    the order detectors happened to run, or every diff becomes noise."""
    ordered = sorted(
        findings,
        key=lambda f: (f.subject_id or "~", f.visit_id or "~", f.detector,
                       f.record_ids[0] if f.record_ids else ""),
    )
    return [f.with_id(f"FND-{index:04d}") for index, f in enumerate(ordered, start=start)]
