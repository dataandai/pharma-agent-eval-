"""Loading and exact indexing of one study's records.

No vector store, no embeddings, no similarity. Every lookup here is an exact
match on a normalised key. An approximate retrieval miss does not merely lose a
row here: it fabricates a safety finding, or hides one.

Normalisation happens once, on load, and every normalisation is recorded rather
than performed silently -- the count of records that matched only after
normalisation is itself a finding about the site.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from src.dates import ClinicalDate, parse_clinical_date
from src.dosing import JsonWeightSource, WeightSource, normalise_subject_id
from src.protocol import (
    ProtocolCatalogue,
    Subject,
    load_protocol,
    load_subjects,
    normalise_status,
)
from src.quantities import is_missing


def _clean_text(raw: object) -> str | None:
    """Free text, or None if the value is one of the missing markers."""
    if is_missing(raw):
        return None
    text = str(raw).strip()
    return text or None


def _normalise_assessments(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(sorted(
        str(item).strip().lower() for item in raw if not is_missing(item)
    ))


@dataclass(frozen=True)
class VisitRecord:
    record_id: str
    subject_id: str | None
    raw_subject_id: Any
    site_id: str | None
    visit_id: str
    visit_label: str
    visit_date: ClinicalDate
    assessments_done: tuple[str, ...]
    status: str | None
    entered_date: ClinicalDate
    comment: str | None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def id_was_normalised(self) -> bool:
        return (self.subject_id is not None
                and str(self.raw_subject_id) != self.subject_id)

    @property
    def entry_lag_days(self) -> int | None:
        if self.visit_date.is_exact and self.entered_date.is_exact:
            return (self.entered_date.exact - self.visit_date.exact).days
        return None


@dataclass(frozen=True)
class DoseRecord:
    record_id: str
    subject_id: str | None
    raw_subject_id: Any
    visit_id: str
    dose_date: ClinicalDate
    raw_dose: Any
    dose_unit: Any
    dose_status: str | None
    reason: str | None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def was_withheld(self) -> bool:
        return (self.dose_status or "").strip().lower() in {"withheld", "held", "not given"}

    @property
    def id_was_normalised(self) -> bool:
        return (self.subject_id is not None
                and str(self.raw_subject_id) != self.subject_id)


@dataclass(frozen=True)
class LoggedDeviation:
    deviation_id: str
    subject_id: str | None
    site_id: str | None
    visit_id: str | None
    category: str
    classification: str | None
    description: str
    raw: dict = field(default_factory=dict, repr=False)


@dataclass
class Study:
    catalogue: ProtocolCatalogue
    subjects: dict[str, Subject]
    visits: tuple[VisitRecord, ...]
    doses: tuple[DoseRecord, ...]
    deviation_log: tuple[LoggedDeviation, ...]
    weights: WeightSource
    as_of: date

    _visits_by_subject: dict[str, list[VisitRecord]] = field(default_factory=dict, repr=False)
    _doses_by_subject: dict[str, list[DoseRecord]] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        by_subject: dict[str, list[VisitRecord]] = defaultdict(list)
        for record in self.visits:
            if record.subject_id:
                by_subject[record.subject_id].append(record)
        self._visits_by_subject = dict(by_subject)

        doses: dict[str, list[DoseRecord]] = defaultdict(list)
        for record in self.doses:
            if record.subject_id:
                doses[record.subject_id].append(record)
        self._doses_by_subject = dict(doses)

    # -- exact lookups -----------------------------------------------------
    def visits_for(self, subject_id: str) -> list[VisitRecord]:
        return list(self._visits_by_subject.get(subject_id, ()))

    def visit_records(self, subject_id: str, visit_id: str) -> list[VisitRecord]:
        """A list, not a single record: the same visit can be entered twice."""
        return [r for r in self.visits_for(subject_id) if r.visit_id == visit_id]

    def doses_for(self, subject_id: str) -> list[DoseRecord]:
        return list(self._doses_by_subject.get(subject_id, ()))

    def dose_records(self, subject_id: str, visit_id: str) -> list[DoseRecord]:
        return [r for r in self.doses_for(subject_id) if r.visit_id == visit_id]

    def logged_deviation(self, subject_id: str, visit_id: str,
                         category: str) -> LoggedDeviation | None:
        """An already-recorded deviation must not be filed a second time.

        Double-reporting inflates the deviation rate the sponsor reports to the
        regulator, and buries the entry the investigator already reviewed."""
        for entry in self.deviation_log:
            if (entry.subject_id == subject_id
                    and entry.visit_id == visit_id
                    and entry.category == category):
                return entry
        return None

    # -- records that could not be attributed ------------------------------
    @property
    def unattributable_visits(self) -> list[VisitRecord]:
        return [r for r in self.visits if r.subject_id is None]

    @property
    def unknown_subject_visits(self) -> list[VisitRecord]:
        return [r for r in self.visits
                if r.subject_id is not None and r.subject_id not in self.subjects]

    @property
    def unknown_subject_doses(self) -> list[DoseRecord]:
        return [r for r in self.doses
                if r.subject_id is None or r.subject_id not in self.subjects]

    def enrolled_subjects(self) -> list[Subject]:
        """Screen failures excluded. Protocol compliance does not apply to a
        subject who was never enrolled."""
        return [s for s in sorted(self.subjects.values(), key=lambda s: s.subject_id)
                if s.is_enrolled]

    @property
    def normalised_id_count(self) -> int:
        return sum(1 for r in self.visits if r.id_was_normalised) + \
               sum(1 for r in self.doses if r.id_was_normalised)

    @classmethod
    def load(cls, data_dir: Path | str, as_of: date | None = None) -> "Study":
        root = Path(data_dir)
        visits = tuple(
            VisitRecord(
                record_id=str(row.get("visit_record_id", "?")),
                subject_id=normalise_subject_id(row.get("subject_id")),
                raw_subject_id=row.get("subject_id"),
                site_id=row.get("site_id"),
                visit_id=str(row.get("visit_id", "")),
                visit_label=str(row.get("visit_label", "")),
                visit_date=parse_clinical_date(row.get("visit_date")),
                assessments_done=_normalise_assessments(row.get("assessments_done")),
                status=normalise_status(row.get("status")),
                entered_date=parse_clinical_date(row.get("entered_date")),
                comment=_clean_text(row.get("comment")),
                raw=row,
            )
            for row in json.loads((root / "visits.json").read_text(encoding="utf-8"))
        )
        doses = tuple(
            DoseRecord(
                record_id=str(row.get("dosing_record_id", "?")),
                subject_id=normalise_subject_id(row.get("subject_id")),
                raw_subject_id=row.get("subject_id"),
                visit_id=str(row.get("visit_id", "")),
                dose_date=parse_clinical_date(row.get("dose_date")),
                raw_dose=row.get("dose_administered"),
                dose_unit=row.get("dose_unit"),
                dose_status=_clean_text(row.get("dose_status")),
                reason=_clean_text(row.get("reason")),
                raw=row,
            )
            for row in json.loads((root / "dosing.json").read_text(encoding="utf-8"))
        )
        logged = tuple(
            LoggedDeviation(
                deviation_id=str(row.get("deviation_id", "?")),
                subject_id=normalise_subject_id(row.get("subject_id")),
                site_id=row.get("site_id"),
                visit_id=str(row["visit_id"]) if row.get("visit_id") else None,
                category=str(row.get("category", "")),
                classification=_clean_text(row.get("classification")),
                description=str(row.get("description", "")),
                raw=row,
            )
            for row in json.loads((root / "deviation_log.json").read_text(encoding="utf-8"))
        )
        return cls(
            catalogue=load_protocol(root / "protocol.json"),
            subjects=load_subjects(root / "subjects.json"),
            visits=visits,
            doses=doses,
            deviation_log=logged,
            weights=JsonWeightSource(root / "vitals.json"),
            as_of=as_of or date.today(),
        )
