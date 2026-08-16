"""Dose normalisation with provenance.

A conversion factor that depends on a date, where using the wrong date
produces a plausible, wrong answer.

A mg/kg dose depends on body weight, and weight changes across a study. The
expected dose at the Week 12 visit uses the Week 12 weight -- not the screening
weight and not the latest one on file. Using the wrong anchor fabricates a
deviation that looks entirely reasonable.

Four rules:

- **As-of lookup.** Most recent measurement on or before the visit date. A
  measurement dated after the visit is never used. When the match is not exact,
  say so: `exact_date_match=False` is provenance, not a footnote.
- **Never guess.** A missing weight, a missing unit, an implausible value or an
  imprecise date makes the dose *not assessable*. It does not make it zero, it
  does not make it a deviation, and it is not an invitation to substitute a
  neighbouring measurement silently.
- **Round once, at the end.** Convert at full precision and quantize on the way
  out.
- **Structured result, never an exception.** Nothing here raises into the graph,
  and nothing returns a bare number. `explanation` is what the agent shows the
  user, built from real figures and real record IDs, so the model never has to
  produce one.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Protocol

from src.quantities import (
    IncompatibleUnits,
    Quantity,
    QuantityError,
    UnknownUnit,
    Unparseable,
    bsa_mosteller,
    canonical_unit,
    is_missing,
    parse_decimal,
    parse_quantity,
)

# A body weight outside this range is a data entry error, not a small or large
# adult. 8.16 is a decimal slip of 81.6; 1800 is pounds in a kilogram field.
PLAUSIBLE_WEIGHT_KG: tuple[Decimal, Decimal] = (Decimal("30"), Decimal("250"))
PLAUSIBLE_HEIGHT_CM: tuple[Decimal, Decimal] = (Decimal("120"), Decimal("230"))

_TRAILING_DIGITS = re.compile(r"(\d+)\s*$")


def normalise_subject_id(raw: object) -> str | None:
    """Collapse one site's several spellings of a subject ID into one.

    SITE-03 writes S007, 007, SITE03-007 and s-007 for the same person; the
    other sites are consistent. Returns None when there is nothing to normalise,
    which is a finding in itself rather than a value to guess at.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    match = _TRAILING_DIGITS.search(text)
    if not match:
        return None
    return f"S-{int(match.group(1)):03d}"


# --------------------------------------------------------------------------
# Weight observations
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class WeightObservation:
    """One weight measurement, already judged usable or not."""

    record_id: str
    subject_id: str | None
    measured_date: date | None
    raw_date: Any
    weight_kg: Decimal | None
    raw_weight: Any
    raw_unit: Any
    height_cm: Decimal | None
    problem: str | None = None

    @property
    def usable(self) -> bool:
        return self.problem is None and self.weight_kg is not None


def _parse_iso(raw: object) -> date | None:
    """Full ISO dates only. '2025-06', '2025-06-UN' and '2025' are real values
    but they cannot be ordered against a visit date, so they are not usable for
    an as-of lookup."""
    if not isinstance(raw, str) or len(raw.strip()) != 10:
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def _height(raw: object) -> Decimal | None:
    try:
        value = parse_decimal(raw)
    except QuantityError:
        return None
    if value is None:
        return None
    low, high = PLAUSIBLE_HEIGHT_CM
    return value if low <= value <= high else None


def build_observation(record: dict) -> WeightObservation:
    """Judge one vitals row. Every rejection carries its reason."""
    record_id = str(record.get("vitals_record_id") or record.get("record_id") or "?")
    subject_id = normalise_subject_id(record.get("subject_id"))
    measured = _parse_iso(record.get("measured_date"))
    raw_weight = record.get("weight")
    raw_unit = record.get("weight_unit")
    height = _height(record.get("height_cm"))

    def rejected(problem: str, weight_kg: Decimal | None = None) -> WeightObservation:
        return WeightObservation(record_id, subject_id, measured, record.get("measured_date"),
                                 weight_kg, raw_weight, raw_unit, height, problem)

    if subject_id is None:
        return rejected("unattributable_record")
    if measured is None:
        return rejected("imprecise_measurement_date")
    if is_missing(raw_weight):
        return rejected("missing_weight")
    if is_missing(raw_unit) and not isinstance(raw_weight, str):
        return rejected("missing_weight_unit")

    try:
        quantity = parse_quantity(raw_weight, raw_unit)
    except UnknownUnit:
        return rejected("missing_weight_unit")
    except (Unparseable, IncompatibleUnits):
        return rejected("unparseable_weight")
    if quantity is None:
        return rejected("missing_weight")

    try:
        kilograms = quantity.convert_to("kg").value
    except QuantityError:
        return rejected("weight_not_a_mass")

    low, high = PLAUSIBLE_WEIGHT_KG
    if not (low <= kilograms <= high):
        return rejected("implausible_weight", kilograms)

    return WeightObservation(record_id, subject_id, measured, record.get("measured_date"),
                             kilograms, raw_weight, raw_unit, height, None)


class WeightSource(Protocol):
    def observations_for(self, subject_id: str) -> list[WeightObservation]:
        ...


@dataclass
class JsonWeightSource:
    """Reads vitals.json. Exact indexing on a normalised subject ID -- no
    vector store, no fuzzy match. An approximate retrieval miss here fabricates
    a safety finding."""

    path: Path
    _index: dict[str, list[WeightObservation]] = field(default_factory=dict, init=False)
    _loaded: bool = field(default=False, init=False)

    def _load(self) -> None:
        if self._loaded:
            return
        rows = json.loads(Path(self.path).read_text(encoding="utf-8"))
        for row in rows:
            observation = build_observation(row)
            if observation.subject_id is None:
                continue
            self._index.setdefault(observation.subject_id, []).append(observation)
        for observations in self._index.values():
            observations.sort(key=lambda o: (o.measured_date or date.min, o.record_id))
        self._loaded = True

    def observations_for(self, subject_id: str) -> list[WeightObservation]:
        self._load()
        key = normalise_subject_id(subject_id)
        return list(self._index.get(key, [])) if key else []


@dataclass
class InMemoryWeightSource:
    """For tests and for callers that already hold the rows."""

    rows: Iterable[dict]

    def observations_for(self, subject_id: str) -> list[WeightObservation]:
        key = normalise_subject_id(subject_id)
        found = [build_observation(row) for row in self.rows]
        found = [o for o in found if o.subject_id == key]
        found.sort(key=lambda o: (o.measured_date or date.min, o.record_id))
        return found


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class DoseResult:
    dose: Decimal
    unit: str
    weight_used: Decimal | None
    weight_date: date | None
    exact_date_match: bool
    method: str
    explanation: str
    weight_record_id: str | None = None

    ok: bool = True

    def to_dict(self) -> dict:
        return {
            "ok": True,
            "dose": str(self.dose),
            "unit": self.unit,
            "weight_used": str(self.weight_used) if self.weight_used is not None else None,
            "weight_date": self.weight_date.isoformat() if self.weight_date else None,
            "exact_date_match": self.exact_date_match,
            "method": self.method,
            "explanation": self.explanation,
            "weight_record_id": self.weight_record_id,
        }


@dataclass(frozen=True)
class DoseError:
    code: str
    message: str
    explanation: str
    subject_id: str
    on_date: date | None = None
    detail: dict = field(default_factory=dict)

    ok: bool = False

    def to_dict(self) -> dict:
        return {
            "ok": False,
            "code": self.code,
            "message": self.message,
            "explanation": self.explanation,
            "subject_id": self.subject_id,
            "on_date": self.on_date.isoformat() if self.on_date else None,
            "detail": self.detail,
        }


PROBLEM_EXPLANATIONS = {
    "missing_weight": "the weight field is empty or holds a missing-value marker",
    "missing_weight_unit": "the weight has no unit, and assuming kilograms is not safe "
                           "at a site that records pounds",
    "unparseable_weight": "the weight is present but is not a number",
    "implausible_weight": f"the weight is outside the plausible range "
                          f"{PLAUSIBLE_WEIGHT_KG[0]}-{PLAUSIBLE_WEIGHT_KG[1]} kg",
    "weight_not_a_mass": "the weight is recorded in a unit that is not a mass",
    "imprecise_measurement_date": "the measurement date is not precise to the day, so it "
                                  "cannot be ordered against the visit date",
    "unattributable_record": "the record has no usable subject identifier",
}


# --------------------------------------------------------------------------
# Weight resolution
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class WeightResolution:
    observation: WeightObservation | None
    exact_date_match: bool
    rejected_on_date: WeightObservation | None
    considered: int


def resolve_weight(
    observations: list[WeightObservation], on_date: date
) -> WeightResolution:
    """Most recent usable measurement on or before `on_date`.

    A measurement dated after the visit is never used, however close it is. If a
    measurement exists on the visit date but is unusable, that is reported
    rather than quietly stepped over -- silently reaching back to an earlier
    visit's weight is an undeclared imputation.
    """
    on_or_before = [o for o in observations
                    if o.measured_date is not None and o.measured_date <= on_date]

    rejected_on_date = next(
        (o for o in observations if o.measured_date == on_date and not o.usable), None
    )
    if rejected_on_date is not None:
        return WeightResolution(None, False, rejected_on_date, len(observations))

    exact = next((o for o in on_or_before if o.measured_date == on_date and o.usable), None)
    if exact is not None:
        return WeightResolution(exact, True, None, len(observations))

    earlier = [o for o in on_or_before if o.usable]
    if not earlier:
        return WeightResolution(None, False, None, len(observations))
    return WeightResolution(earlier[-1], False, None, len(observations))


# --------------------------------------------------------------------------
# The public entry point
# --------------------------------------------------------------------------

def normalise_dose(
    value: object,
    from_unit: object,
    to_unit: object,
    subject_id: str,
    on_date: date,
    *,
    source: WeightSource,
    places: int = 0,
) -> DoseResult | DoseError:
    """Convert a dose between units for one subject on one date.

    The conversion factor -- the subject's weight -- is looked up as of the
    visit date, not as of today, and the provenance of that lookup travels with
    the answer.
    """
    canonical_subject = normalise_subject_id(subject_id)
    if canonical_subject is None:
        return DoseError(
            code="unattributable_subject",
            message=f"cannot resolve a subject from {subject_id!r}",
            explanation=f"The subject reference {subject_id!r} cannot be resolved to a "
                        f"subject. No dose can be computed and none should be guessed.",
            subject_id=str(subject_id),
            on_date=on_date,
        )

    try:
        source_unit = canonical_unit(from_unit)
        target_unit = canonical_unit(to_unit)
    except UnknownUnit as exc:
        return DoseError(
            code="unknown_unit",
            message=str(exc),
            explanation=f"Cannot compute a dose for {canonical_subject} on "
                        f"{on_date.isoformat()}: {exc}. A missing or unrecognised unit is a "
                        f"question for the site, not a value to assume.",
            subject_id=canonical_subject,
            on_date=on_date,
            detail={"from_unit": from_unit, "to_unit": to_unit},
        )

    try:
        amount = parse_decimal(value)
    except QuantityError as exc:
        return DoseError(
            code="unparseable_value",
            message=str(exc),
            explanation=f"Cannot compute a dose for {canonical_subject} on "
                        f"{on_date.isoformat()}: the value {value!r} is not a number.",
            subject_id=canonical_subject, on_date=on_date, detail={"value": repr(value)},
        )
    if amount is None:
        return DoseError(
            code="missing_value",
            message="value is missing",
            explanation=f"Cannot compute a dose for {canonical_subject} on "
                        f"{on_date.isoformat()}: the value is missing "
                        f"(recorded as {value!r}).",
            subject_id=canonical_subject, on_date=on_date, detail={"value": repr(value)},
        )

    quantity = Quantity(amount, source_unit)

    # Straight mass conversion, or a no-op. No weight needed.
    if source_unit == target_unit or (
        quantity.dimension == "mass" and canonical_unit(target_unit) in ("mcg", "mg", "g", "kg", "lb")
        and target_unit != source_unit
    ):
        converted = quantity.convert_to(target_unit)
        rounded = converted.quantize(places)
        if source_unit == target_unit:
            explanation = (f"{amount} {source_unit} for {canonical_subject} on "
                           f"{on_date.isoformat()}; no conversion required.")
            method = "identity"
        else:
            explanation = (f"{amount} {source_unit} = {converted.value} {target_unit} "
                           f"(exact unit conversion), rounded once at the end to "
                           f"{rounded.value} {target_unit}.")
            method = "mass_conversion"
        return DoseResult(
            dose=rounded.value, unit=target_unit, weight_used=None, weight_date=None,
            exact_date_match=True, method=method, explanation=explanation,
        )

    # Everything else needs the subject's weight as of that date.
    observations = source.observations_for(canonical_subject)
    if not observations:
        return DoseError(
            code="no_weight_records",
            message=f"no weight records for {canonical_subject}",
            explanation=f"Cannot compute the expected dose for {canonical_subject} on "
                        f"{on_date.isoformat()}: no weight measurements exist for this "
                        f"subject at all.",
            subject_id=canonical_subject, on_date=on_date,
        )

    resolution = resolve_weight(observations, on_date)

    if resolution.rejected_on_date is not None:
        bad = resolution.rejected_on_date
        reason = PROBLEM_EXPLANATIONS.get(bad.problem, bad.problem or "unusable")
        return DoseError(
            code="weight_unusable",
            message=f"weight record {bad.record_id} is unusable: {bad.problem}",
            explanation=(
                f"Cannot compute the expected dose for {canonical_subject} on "
                f"{on_date.isoformat()}. A weight was recorded on that date "
                f"({bad.record_id}, value {bad.raw_weight!r}"
                f"{f', unit {bad.raw_unit!r}' if bad.raw_unit is not None else ', no unit field'})"
                f", but it cannot be used: {reason}. An earlier visit's weight is not this "
                f"visit's weight, so substituting one would be an undeclared imputation. "
                f"This dose is not assessable until the site supplies the measurement."
            ),
            subject_id=canonical_subject, on_date=on_date,
            detail={"record_id": bad.record_id, "problem": bad.problem,
                    "raw_weight": repr(bad.raw_weight), "raw_unit": repr(bad.raw_unit)},
        )

    if resolution.observation is None:
        later = [o for o in observations
                 if o.measured_date is not None and o.measured_date > on_date]
        hint = ""
        if later:
            nearest = min(later, key=lambda o: o.measured_date)
            hint = (f" The nearest measurement ({nearest.record_id}) is dated "
                    f"{nearest.measured_date.isoformat()}, after the visit, and a weight "
                    f"recorded after the fact cannot be used to judge the dose given before it.")
        return DoseError(
            code="no_weight_on_or_before",
            message=f"no usable weight on or before {on_date.isoformat()}",
            explanation=(
                f"Cannot compute the expected dose for {canonical_subject} on "
                f"{on_date.isoformat()}: none of the {resolution.considered} weight record(s) "
                f"for this subject is both usable and dated on or before that day.{hint}"
            ),
            subject_id=canonical_subject, on_date=on_date,
            detail={"considered": resolution.considered,
                    "rejected": [{"record_id": o.record_id, "problem": o.problem}
                                 for o in observations if not o.usable]},
        )

    weight = resolution.observation
    return _convert_with_weight(
        quantity, target_unit, canonical_subject, on_date, weight,
        resolution.exact_date_match, places,
    )


def _convert_with_weight(
    quantity: Quantity,
    target_unit: str,
    subject_id: str,
    on_date: date,
    weight: WeightObservation,
    exact: bool,
    places: int,
) -> DoseResult | DoseError:
    kilograms = weight.weight_kg
    steps: list[str] = []

    # Show the unit conversion when the site did not record kilograms -- this is
    # the line that stops a correct 408 mg looking like a 55% underdose.
    original = parse_quantity(weight.raw_weight, weight.raw_unit)
    if original is not None and original.unit != "kg":
        steps.append(f"{original.value} {original.unit} x "
                     f"{Decimal('0.45359237') if original.unit == 'lb' else '(unit factor)'} "
                     f"= {kilograms} kg")

    source_unit = quantity.unit

    try:
        if source_unit == "mg/kg" and target_unit in ("mcg", "mg", "g", "kg"):
            exact_value = quantity.value * kilograms
            steps.append(f"{quantity.value} mg/kg x {kilograms} kg = {exact_value} mg")
            result = Quantity(exact_value, "mg").convert_to(target_unit)
            method = "mg_per_kg"

        elif source_unit in ("mcg", "mg", "g", "kg") and target_unit == "mg/kg":
            in_mg = quantity.convert_to("mg").value
            exact_value = in_mg / kilograms
            steps.append(f"{in_mg} mg / {kilograms} kg = {exact_value} mg/kg")
            result = Quantity(exact_value, "mg/kg")
            method = "mg_per_kg"

        elif source_unit == "mg/m2" or target_unit == "mg/m2":
            if weight.height_cm is None:
                return DoseError(
                    code="missing_height",
                    message="mg/m2 needs a height for the BSA calculation",
                    explanation=(
                        f"Cannot compute a mg/m2 dose for {subject_id} on "
                        f"{on_date.isoformat()}: body surface area needs a height, and record "
                        f"{weight.record_id} has none that is usable."
                    ),
                    subject_id=subject_id, on_date=on_date,
                    detail={"record_id": weight.record_id},
                )
            bsa = bsa_mosteller(kilograms, weight.height_cm)
            steps.append(f"BSA (Mosteller) = sqrt({weight.height_cm} cm x {kilograms} kg "
                         f"/ 3600) = {bsa.quantize(Decimal('0.0001'))} m2")
            if source_unit == "mg/m2":
                exact_value = quantity.value * bsa
                steps.append(f"{quantity.value} mg/m2 x {bsa.quantize(Decimal('0.0001'))} m2 "
                             f"= {exact_value} mg")
                result = Quantity(exact_value, "mg").convert_to(target_unit)
            else:
                in_mg = quantity.convert_to("mg").value
                exact_value = in_mg / bsa
                steps.append(f"{in_mg} mg / {bsa.quantize(Decimal('0.0001'))} m2 "
                             f"= {exact_value} mg/m2")
                result = Quantity(exact_value, "mg/m2")
            method = "mg_per_m2_mosteller"

        else:
            return DoseError(
                code="incompatible_units",
                message=f"cannot convert {source_unit} to {target_unit}",
                explanation=f"Cannot convert {source_unit} to {target_unit} for {subject_id}.",
                subject_id=subject_id, on_date=on_date,
                detail={"from_unit": source_unit, "to_unit": target_unit},
            )
    except QuantityError as exc:
        return DoseError(
            code="conversion_failed", message=str(exc),
            explanation=f"Cannot compute a dose for {subject_id} on {on_date.isoformat()}: {exc}",
            subject_id=subject_id, on_date=on_date,
        )

    rounded = result.quantize(places)

    if exact:
        provenance = (f"Weight {weight.raw_weight} {weight.raw_unit or ''}".rstrip() +
                      f" recorded on {weight.measured_date.isoformat()} "
                      f"({weight.record_id}), the measurement taken on the visit date.")
    else:
        provenance = (f"Weight {weight.raw_weight} {weight.raw_unit or ''}".rstrip() +
                      f" recorded on {weight.measured_date.isoformat()} "
                      f"({weight.record_id}). No weight was recorded on "
                      f"{on_date.isoformat()} itself, so the most recent measurement on or "
                      f"before that date was used.")

    explanation = (
        f"{provenance} " + " ".join(f"{step}." for step in steps) +
        f" Rounded once at the end: {rounded.value} {rounded.unit}."
    )

    return DoseResult(
        dose=rounded.value,
        unit=rounded.unit,
        weight_used=kilograms,
        weight_date=weight.measured_date,
        exact_date_match=exact,
        method=method,
        explanation=explanation,
        weight_record_id=weight.record_id,
    )
