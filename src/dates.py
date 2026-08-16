"""Clinical dates, including the ones that are not dates.

A partial date is not a broken date -- it is an *interval*. "2025-06" means
"some day in June 2025", which is a perfectly good piece of information; it just
cannot be compared to a +/-3 day window the way a full date can. An ambiguous
date like "05/06/2025" is a *set* of two candidate days.

Modelling both as "the set of days this record could refer to" makes the three
verdicts fall out arithmetically rather than by special case:

- every candidate day inside the window  -> compliant
- every candidate day outside the window -> deviation
- some in, some out                      -> not_assessable

That middle case is the interesting one. A visit recorded as "2025-10" against a
window in August is still unambiguously late, and calling it not_assessable
would be as wrong as imputing a day. The interval decides.

Grounded in CDISC guidance on incomplete dates: always reflect what is known;
never infer missing data without justification. Nothing here imputes. Where a
caller wants an imputed day it must ask for one explicitly and carry the flag.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date
try:
    from enum import StrEnum
except Exception:  # pragma: no cover - compatibility for Python <3.11
    from enum import Enum

    class StrEnum(str, Enum):
        pass

from src.quantities import is_missing


class Precision(StrEnum):
    DAY = "day"
    MONTH = "month"
    YEAR = "year"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


_ISO_DAY = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_ISO_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
_ISO_MONTH_UNKNOWN_DAY = re.compile(r"^(\d{4})-(\d{2})-(?:UN|UNK|XX|--)$", re.IGNORECASE)
_ISO_YEAR = re.compile(r"^(\d{4})$")
_ISO_YEAR_UNKNOWN = re.compile(r"^(\d{4})-(?:UN|UNK|XX|--)(?:-(?:UN|UNK|XX|--))?$", re.IGNORECASE)
_SLASHED = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


@dataclass(frozen=True)
class ClinicalDate:
    """A date, or the set of days a partial or ambiguous record could mean."""

    raw: object
    precision: Precision
    candidates: tuple[tuple[date, date], ...]   # inclusive (earliest, latest) spans
    note: str = ""

    @property
    def known(self) -> bool:
        return bool(self.candidates)

    @property
    def is_exact(self) -> bool:
        return self.precision is Precision.DAY

    @property
    def exact(self) -> date | None:
        """The single day this refers to, or None if it is not precise."""
        if self.precision is Precision.DAY:
            return self.candidates[0][0]
        return None

    @property
    def earliest(self) -> date | None:
        return min(span[0] for span in self.candidates) if self.candidates else None

    @property
    def latest(self) -> date | None:
        return max(span[1] for span in self.candidates) if self.candidates else None

    def possible_days_within(self, opens: date, closes: date) -> tuple[bool, bool]:
        """(any candidate inside the window, any candidate outside it)."""
        inside = outside = False
        for span_start, span_end in self.candidates:
            if span_start <= closes and span_end >= opens:
                inside = True
            if span_start < opens or span_end > closes:
                outside = True
        return inside, outside

    def entirely_before(self, other: date) -> bool:
        return self.known and self.latest < other

    def entirely_after(self, other: date) -> bool:
        return self.known and self.earliest > other

    def describe(self) -> str:
        if not self.known:
            return f"{self.raw!r} (no usable date)"
        if self.precision is Precision.DAY:
            return self.exact.isoformat()
        if self.precision is Precision.AMBIGUOUS:
            readings = " or ".join(span[0].isoformat() for span in self.candidates)
            return f"{self.raw!r} (ambiguous: {readings})"
        return (f"{self.raw!r} ({self.precision.value} precision: "
                f"{self.earliest.isoformat()} to {self.latest.isoformat()})")


def _span_for_month(year: int, month: int) -> tuple[date, date]:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last)


def _unknown(raw: object, note: str) -> ClinicalDate:
    return ClinicalDate(raw=raw, precision=Precision.UNKNOWN, candidates=(), note=note)


def parse_clinical_date(raw: object) -> ClinicalDate:
    """Parse the date forms this dataset actually contains.

    Never raises and never imputes. An unreadable value comes back as
    Precision.UNKNOWN with a note saying why.
    """
    if is_missing(raw):
        return _unknown(raw, "value is missing")
    if isinstance(raw, date):
        return ClinicalDate(raw, Precision.DAY, ((raw, raw),))
    if not isinstance(raw, str):
        return _unknown(raw, f"not a date: {type(raw).__name__}")

    text = raw.strip()
    if not text:
        return _unknown(raw, "value is empty")

    match = _ISO_DAY.match(text)
    if match:
        try:
            day = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return _unknown(raw, "not a real calendar date")
        return ClinicalDate(raw, Precision.DAY, ((day, day),))

    for pattern in (_ISO_MONTH, _ISO_MONTH_UNKNOWN_DAY):
        match = pattern.match(text)
        if match:
            year, month = int(match.group(1)), int(match.group(2))
            if not 1 <= month <= 12:
                return _unknown(raw, "month out of range")
            span = _span_for_month(year, month)
            return ClinicalDate(raw, Precision.MONTH, (span,),
                                note="day of month not recorded")

    for pattern in (_ISO_YEAR, _ISO_YEAR_UNKNOWN):
        match = pattern.match(text)
        if match:
            year = int(match.group(1))
            return ClinicalDate(raw, Precision.YEAR,
                                ((date(year, 1, 1), date(year, 12, 31)),),
                                note="only the year was recorded")

    match = _SLASHED.match(text)
    if match:
        first, second, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
        readings = []
        for day_value, month_value, convention in (
            (first, second, "DD/MM/YYYY"), (second, first, "MM/DD/YYYY")
        ):
            try:
                readings.append((date(year, month_value, day_value), convention))
            except ValueError:
                continue
        if not readings:
            return _unknown(raw, "neither DD/MM nor MM/DD gives a real date")
        # Deduplicate the case where both readings coincide (e.g. 05/05/2025).
        unique = sorted({day for day, _ in readings})
        if len(unique) == 1:
            return ClinicalDate(raw, Precision.DAY, ((unique[0], unique[0]),),
                                note="slashed format, but both readings agree")
        return ClinicalDate(
            raw, Precision.AMBIGUOUS,
            tuple((day, day) for day in unique),
            note="the file carries no locale declaration and both readings are real dates: "
                 + " or ".join(f"{day.isoformat()} ({conv})" for day, conv in
                               sorted(readings, key=lambda r: r[0])),
        )

    return _unknown(raw, "unrecognised date format")


def days_between(earlier: ClinicalDate, later: ClinicalDate) -> tuple[int, int] | None:
    """Range of possible day counts from `earlier` to `later`.

    Returns None if either side is unusable. When both are exact the range is a
    single number repeated, which is what lets callers treat exact and partial
    dates through one path.
    """
    if not (earlier.known and later.known):
        return None
    return ((later.earliest - earlier.latest).days,
            (later.latest - earlier.earliest).days)
