"""Protocol version lineage and per-subject visit windows.

Two pieces of arithmetic that look trivial and are not.

**Which version governs a subject.** The version they consented under -- not the
version in force on the calendar date of the visit. A subject who consented
under v1.0 three weeks before v2.0 took effect keeps v1.0's windows for the
whole study. Measuring them against v2.0 suppresses real deviations; measuring a
v2.0 subject against v1.0 fabricates them. In this dataset S-004 and S-009 are
late by exactly the same number of days and must reach opposite verdicts.

**Where the window falls.** Day 1 is the subject's first dose, and it is a
different calendar date for every subject. "Day 29 +/-3" is therefore a
different window per subject. Computing it from the enrolment or consent date
instead silently shifts every window in the study.

One shape underlies both: use the value as of the relevant anchor, not the one
that happens to be nearest to hand.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

from src.dates import ClinicalDate, Precision, parse_clinical_date
from src.dosing import normalise_subject_id
from src.verdicts import Verdict

# Layer A6: the same enrolment state arrives spelled several ways.
ENROLMENT_STATUS_MAP = {
    "randomized": "randomized", "randomised": "randomized", "rand": "randomized",
    "completed": "completed", "complete": "completed",
    "screen failure": "screen_failure", "screenfailure": "screen_failure",
    "screen_failure": "screen_failure", "sf": "screen_failure",
    "withdrawn": "withdrawn", "discontinued": "withdrawn",
}


def normalise_status(raw: object) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip().lower()
    return ENROLMENT_STATUS_MAP.get(text, text or None)


# --------------------------------------------------------------------------
# Protocol
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ScheduledVisit:
    """One visit of the protocol schedule, resolved onto one subject's calendar."""

    visit_id: str
    label: str
    target_day: int | None
    window_before: int | None
    window_after: int | None
    required_assessments: tuple[str, ...]
    target_date: date | None
    opens: date
    closes: date
    is_screening: bool = False

    @property
    def window_text(self) -> str:
        if self.is_screening:
            return f"day -{(self.opens - self.closes).days * -1 + 1} to -1 relative to Day 1"
        if self.window_before == self.window_after:
            return f"+/-{self.window_after}"
        return f"-{self.window_before}/+{self.window_after}"

    def describe(self) -> str:
        if self.is_screening:
            return (f"{self.label}: {self.opens.isoformat()} to {self.closes.isoformat()} "
                    f"(the 28 days before Day 1)")
        return (f"{self.label} (protocol day {self.target_day}, {self.window_text}): "
                f"target {self.target_date.isoformat()}, window "
                f"{self.opens.isoformat()} to {self.closes.isoformat()}")


@dataclass(frozen=True)
class ProtocolVersion:
    protocol_id: str
    version: str
    effective_date: date
    amends: str | None
    visit_schedule: tuple[dict[str, Any], ...]
    dosing: dict[str, Any]
    eligibility: dict[str, Any]
    amendment_summary: str | None = None

    def visit(self, visit_id: str) -> dict[str, Any] | None:
        return next((v for v in self.visit_schedule if v["visit_id"] == visit_id), None)

    def required_assessments(self, visit_id: str) -> tuple[str, ...]:
        visit = self.visit(visit_id)
        return tuple(visit["required_assessments"]) if visit else ()

    @property
    def label(self) -> str:
        return f"v{self.version}"


@dataclass(frozen=True)
class ProtocolCatalogue:
    protocol_id: str
    versions: tuple[ProtocolVersion, ...]

    def version(self, label: str) -> ProtocolVersion:
        wanted = str(label).lstrip("vV")
        found = next((v for v in self.versions if v.version == wanted), None)
        if found is None:
            raise KeyError(f"unknown protocol version: {label!r}")
        return found

    def version_in_force_on(self, day: date) -> ProtocolVersion | None:
        """The version in force on a calendar date.

        NOT the governing version for a subject. This exists so a finding can
        say *"v2.0 was in force on that date, but this subject is governed by
        v1.0"* -- the contrast is the explanation. Never use it to pick a
        window.
        """
        eligible = [v for v in self.versions if v.effective_date <= day]
        return max(eligible, key=lambda v: v.effective_date) if eligible else None

    def lineage(self, label: str) -> tuple[ProtocolVersion, ...]:
        """The amendment chain back to the original version."""
        chain, current = [], self.version(label)
        seen = set()
        while current is not None and current.version not in seen:
            seen.add(current.version)
            chain.append(current)
            current = self.version(current.amends) if current.amends else None
        return tuple(reversed(chain))


def load_protocol(path: Path | str) -> ProtocolCatalogue:
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    versions = tuple(
        ProtocolVersion(
            protocol_id=row["protocol_id"],
            version=str(row["version"]),
            effective_date=date.fromisoformat(row["effective_date"]),
            amends=str(row["amends"]) if row.get("amends") else None,
            visit_schedule=tuple(row["visit_schedule"]),
            dosing=row["dosing"],
            eligibility=row["eligibility"],
            amendment_summary=row.get("amendment_summary"),
        )
        for row in rows
    )
    return ProtocolCatalogue(versions[0].protocol_id, versions)


# --------------------------------------------------------------------------
# Subjects
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Subject:
    subject_id: str
    site_id: str
    consent_date: ClinicalDate
    protocol_version_consented: str
    anchor_date: date | None
    enrollment_status: str | None
    screen_failure_reason: str | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_screen_failure(self) -> bool:
        return (self.enrollment_status == "screen_failure"
                or bool(self.screen_failure_reason))

    @property
    def is_enrolled(self) -> bool:
        """A screen failure was never enrolled, so protocol compliance does not
        apply to them. Filing deviations against one is a false positive."""
        return not self.is_screen_failure and self.anchor_date is not None


def load_subjects(path: Path | str) -> dict[str, Subject]:
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    subjects: dict[str, Subject] = {}
    for row in rows:
        subject_id = normalise_subject_id(row.get("subject_id"))
        if subject_id is None:
            continue
        anchor = row.get("anchor_date")
        subjects[subject_id] = Subject(
            subject_id=subject_id,
            site_id=row.get("site_id", ""),
            consent_date=parse_clinical_date(row.get("consent_date")),
            protocol_version_consented=str(row.get("protocol_version_consented", "")),
            anchor_date=date.fromisoformat(anchor) if isinstance(anchor, str) and len(anchor) == 10 else None,
            enrollment_status=normalise_status(row.get("enrollment_status")),
            screen_failure_reason=row.get("screen_failure_reason"),
            raw=row,
        )
    return subjects


# --------------------------------------------------------------------------
# Governing version
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class GoverningVersion:
    subject_id: str
    version: ProtocolVersion
    consented_on: ClinicalDate
    version_in_force: ProtocolVersion | None
    on_date: date | None
    explanation: str

    @property
    def differs_from_calendar_version(self) -> bool:
        """True when a naive 'which version is current?' lookup would have
        picked a different one. This is the trap, made visible."""
        return (self.version_in_force is not None
                and self.version_in_force.version != self.version.version)

    @property
    def precedes_consent(self) -> bool:
        """The date in question falls before the subject consented."""
        return (self.on_date is not None
                and self.consented_on.known
                and self.consented_on.earliest > self.on_date)


def governing_version(
    subject: Subject,
    catalogue: ProtocolCatalogue,
    on_date: date | None = None,
) -> GoverningVersion:
    """The protocol version that governs this subject.

    `on_date` does not select the version -- that is the whole point. It is used
    to report what a calendar lookup *would* have said, and to notice when the
    date in question precedes consent altogether.
    """
    version = catalogue.version(subject.protocol_version_consented)
    in_force = catalogue.version_in_force_on(on_date) if on_date else None

    parts = [
        f"{subject.subject_id} consented on {subject.consent_date.describe()} under "
        f"{version.label}, so {version.label} governs them for the whole study."
    ]
    if in_force is not None and in_force.version != version.version:
        parts.append(
            f"{in_force.label} was in force on {on_date.isoformat()} "
            f"(effective {in_force.effective_date.isoformat()}), but the version in force "
            f"on the calendar date does not govern a subject who consented earlier. "
            f"Measuring {subject.subject_id} against {in_force.label} would change the "
            f"verdict without any change in what the site did."
        )
        if in_force.amendment_summary:
            parts.append(f"{in_force.label} changed: {in_force.amendment_summary}")

    result = GoverningVersion(
        subject_id=subject.subject_id, version=version,
        consented_on=subject.consent_date, version_in_force=in_force,
        on_date=on_date, explanation=" ".join(parts),
    )
    return result


# --------------------------------------------------------------------------
# Per-subject schedule
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Schedule:
    subject_id: str
    version: ProtocolVersion
    anchor_date: date | None
    visits: tuple[ScheduledVisit, ...]
    note: str = ""

    def visit(self, visit_id: str) -> ScheduledVisit | None:
        return next((v for v in self.visits if v.visit_id == visit_id), None)

    @property
    def anchored(self) -> bool:
        return self.anchor_date is not None


def expected_schedule(subject: Subject, catalogue: ProtocolCatalogue) -> Schedule:
    """Per-subject visit windows, computed from that subject's anchor date with
    the per-visit tolerances of their governing version.

    Day 1 is the first dose. Day N is anchor + (N-1) days -- there is no day 0,
    so the off-by-one is real and shifts every window in the study if fumbled.
    """
    version = governing_version(subject, catalogue).version

    if subject.anchor_date is None:
        return Schedule(
            subject_id=subject.subject_id, version=version, anchor_date=None, visits=(),
            note=(f"{subject.subject_id} has no anchor date (no first dose on record), so no "
                  f"visit windows exist. "
                  + (f"They are a screen failure: {subject.screen_failure_reason}. Protocol "
                     f"compliance does not apply to a subject who was never enrolled."
                     if subject.is_screen_failure else
                     "Without a Day 1 no window can be computed for any visit.")),
        )

    anchor = subject.anchor_date
    visits: list[ScheduledVisit] = []
    for entry in version.visit_schedule:
        screening_window = entry.get("screening_window")
        if screening_window is not None:
            opens = anchor + timedelta(days=screening_window["earliest_day"])
            closes = anchor + timedelta(days=screening_window["latest_day"])
            visits.append(ScheduledVisit(
                visit_id=entry["visit_id"], label=entry["label"], target_day=None,
                window_before=None, window_after=None,
                required_assessments=tuple(entry["required_assessments"]),
                target_date=None, opens=opens, closes=closes, is_screening=True,
            ))
            continue

        target = anchor + timedelta(days=entry["target_day"] - 1)
        visits.append(ScheduledVisit(
            visit_id=entry["visit_id"], label=entry["label"],
            target_day=entry["target_day"],
            window_before=entry["window_before"], window_after=entry["window_after"],
            required_assessments=tuple(entry["required_assessments"]),
            target_date=target,
            opens=target - timedelta(days=entry["window_before"]),
            closes=target + timedelta(days=entry["window_after"]),
        ))

    return Schedule(subject.subject_id, version, anchor, tuple(visits))


# --------------------------------------------------------------------------
# Window assessment
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class TimingAssessment:
    verdict: Verdict
    scheduled: ScheduledVisit
    actual: ClinicalDate
    offset_days: tuple[int, int] | None
    calculation: str

    @property
    def days_late(self) -> int | None:
        """Single offset when the date is precise enough to have one."""
        if self.offset_days and self.offset_days[0] == self.offset_days[1]:
            return self.offset_days[0]
        return None


def assess_timing(scheduled: ScheduledVisit, actual: ClinicalDate) -> TimingAssessment:
    """Compare a recorded visit date to its window, three-valued.

    Every candidate day inside the window is compliant; every candidate day
    outside it is a deviation; a mix is not assessable. A month-precision date
    whose whole month falls outside the window is still a deviation -- refusing
    to answer there would be as wrong as imputing a day.
    """
    if not actual.known:
        return TimingAssessment(
            verdict=Verdict.NOT_ASSESSABLE, scheduled=scheduled, actual=actual,
            offset_days=None,
            calculation=(
                f"{scheduled.label} window is {scheduled.opens.isoformat()} to "
                f"{scheduled.closes.isoformat()}. The recorded visit date "
                f"{actual.describe()} cannot be read, so the visit cannot be placed "
                f"inside or outside the window."
            ),
        )

    inside, outside = actual.possible_days_within(scheduled.opens, scheduled.closes)
    reference = scheduled.target_date or scheduled.closes
    offset = ((actual.earliest - reference).days, (actual.latest - reference).days)

    if inside and not outside:
        verdict = Verdict.COMPLIANT
    elif outside and not inside:
        verdict = Verdict.DEVIATION
    else:
        verdict = Verdict.NOT_ASSESSABLE

    window = f"{scheduled.opens.isoformat()} to {scheduled.closes.isoformat()}"
    if scheduled.target_date:
        window = (f"target {scheduled.target_date.isoformat()} {scheduled.window_text} "
                  f"= {window}")

    if actual.is_exact:
        days = offset[0]
        direction = "on target" if days == 0 else (
            f"{abs(days)} day{'s' if abs(days) != 1 else ''} "
            f"{'late' if days > 0 else 'early'}")
        detail = f"performed {actual.exact.isoformat()}, {direction}"
    elif verdict is Verdict.DEVIATION:
        detail = (f"recorded as {actual.describe()}; every day in that range falls outside "
                  f"the window, so the verdict does not depend on the missing detail")
    else:
        detail = (f"recorded as {actual.describe()}; the range straddles the window edge, "
                  f"so the verdict would depend on information the record does not carry")

    return TimingAssessment(
        verdict=verdict, scheduled=scheduled, actual=actual, offset_days=offset,
        calculation=f"{scheduled.label}: {window}; {detail}.",
    )
