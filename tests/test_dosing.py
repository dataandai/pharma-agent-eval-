from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from src.dosing import (
    InMemoryWeightSource,
    JsonWeightSource,
    build_observation,
    normalise_dose,
    normalise_subject_id,
    resolve_weight,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def real_weights():
    return JsonWeightSource(ROOT / "data" / "vitals.json")


def weights(*rows):
    return InMemoryWeightSource(list(rows))


def vitals(record_id, subject_id, measured_date, weight, unit="kg", height="175"):
    return {
        "vitals_record_id": record_id, "subject_id": subject_id,
        "measured_date": measured_date, "weight": weight,
        "weight_unit": unit, "height_cm": height,
    }


# --------------------------------------------------------------------------
# Subject ID normalisation (Layer A3)
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw", ["S-007", "S007", "007", "SITE03-007", "s-007", " s-007 ", "7"],
)
def test_site_03_id_spellings_all_resolve_to_one_subject(raw):
    assert normalise_subject_id(raw) == "S-007"


@pytest.mark.parametrize("raw", ["", None, "   ", "UNKNOWN"])
def test_unattributable_ids_resolve_to_nothing_rather_than_a_guess(raw):
    assert normalise_subject_id(raw) is None


# --------------------------------------------------------------------------
# The trap the prompt names: S-007 in lb resolves to 408 mg
# --------------------------------------------------------------------------

def test_s007_in_pounds_resolves_to_408_mg(real_weights):
    """180 lb = 81.6466266 kg, so 5 mg/kg expects 408 mg. Read as kilograms the
    expectation becomes 900 mg and a correct dose looks like a 55% underdose."""
    result = normalise_dose(
        Decimal("5"), "mg/kg", "mg", "S-007", date(2025, 4, 7), source=real_weights,
    )
    assert result.ok
    assert result.dose == Decimal("408")
    assert result.unit == "mg"
    assert result.exact_date_match is True
    assert result.method == "mg_per_kg"


def test_the_explanation_carries_the_conversion_and_the_record_id(real_weights):
    """The explanation string is what the agent reads out, so the model never
    has to produce a figure."""
    result = normalise_dose(
        Decimal("5"), "mg/kg", "mg", "SITE03-007", date(2025, 4, 7), source=real_weights,
    )
    assert "180" in result.explanation and "lb" in result.explanation
    assert "0.45359237" in result.explanation
    assert "81.6466266" in result.explanation
    assert result.weight_record_id in result.explanation
    assert "408" in result.explanation


def test_reading_the_pound_weight_as_kilograms_would_fabricate_a_deviation(real_weights):
    """Documents the size of the error this module exists to prevent."""
    correct = normalise_dose(
        Decimal("5"), "mg/kg", "mg", "S-007", date(2025, 4, 7), source=real_weights,
    )
    naive = Decimal("5") * Decimal("180")
    apparent_shortfall = (naive - correct.dose) / naive
    assert apparent_shortfall > Decimal("0.54")


# --------------------------------------------------------------------------
# As-of lookup
# --------------------------------------------------------------------------

def test_the_weight_as_of_that_visit_is_used_not_the_screening_weight():
    source = weights(
        vitals("VT-1", "S-100", "2025-01-01", "90.0"),
        vitals("VT-2", "S-100", "2025-03-01", "80.0"),
        vitals("VT-3", "S-100", "2025-06-01", "70.0"),
    )
    result = normalise_dose(Decimal("5"), "mg/kg", "mg", "S-100",
                            date(2025, 3, 1), source=source)
    assert result.dose == Decimal("400")
    assert result.weight_date == date(2025, 3, 1)
    assert result.exact_date_match is True


def test_the_latest_weight_on_file_is_not_used():
    """Using the latest measurement instead of the as-of one produces a
    plausible, wrong expected dose -- and therefore a fabricated deviation."""
    source = weights(
        vitals("VT-1", "S-100", "2025-03-01", "80.0"),
        vitals("VT-2", "S-100", "2025-09-01", "60.0"),
    )
    result = normalise_dose(Decimal("5"), "mg/kg", "mg", "S-100",
                            date(2025, 3, 1), source=source)
    assert result.weight_used == Decimal("80.0")
    assert result.dose == Decimal("400")


def test_a_weight_dated_after_the_visit_is_never_used():
    """A measurement taken after the fact cannot judge a dose given before it."""
    source = weights(vitals("VT-9", "S-100", "2025-06-10", "80.0"))
    result = normalise_dose(Decimal("5"), "mg/kg", "mg", "S-100",
                            date(2025, 6, 1), source=source)
    assert not result.ok
    assert result.code == "no_weight_on_or_before"
    assert "after the visit" in result.explanation
    assert "VT-9" in result.explanation


def test_an_inexact_match_falls_back_and_says_so():
    source = weights(
        vitals("VT-1", "S-100", "2025-03-01", "80.0"),
        vitals("VT-2", "S-100", "2025-05-20", "76.0"),
    )
    result = normalise_dose(Decimal("5"), "mg/kg", "mg", "S-100",
                            date(2025, 6, 1), source=source)
    assert result.ok
    assert result.exact_date_match is False
    assert result.weight_date == date(2025, 5, 20)
    assert result.dose == Decimal("380")
    assert "No weight was recorded on 2025-06-01" in result.explanation


def test_resolution_prefers_the_exact_date_over_a_later_earlier_record():
    source = [
        build_observation(vitals("VT-1", "S-100", "2025-03-01", "80.0")),
        build_observation(vitals("VT-2", "S-100", "2025-03-05", "70.0")),
    ]
    resolved = resolve_weight(source, date(2025, 3, 1))
    assert resolved.observation.record_id == "VT-1"
    assert resolved.exact_date_match is True


# --------------------------------------------------------------------------
# Structured errors, never a guess and never an exception
# --------------------------------------------------------------------------

def test_a_missing_weight_returns_a_structured_error_rather_than_guessing():
    source = weights(vitals("VT-1", "S-100", "2025-06-01", "NA"))
    result = normalise_dose(Decimal("5"), "mg/kg", "mg", "S-100",
                            date(2025, 6, 1), source=source)
    assert not result.ok
    assert result.code == "weight_unusable"
    assert result.detail["problem"] == "missing_weight"
    assert "not assessable" in result.explanation


def test_no_weight_records_at_all_is_its_own_error():
    result = normalise_dose(Decimal("5"), "mg/kg", "mg", "S-404",
                            date(2025, 6, 1), source=weights())
    assert not result.ok
    assert result.code == "no_weight_records"


@pytest.mark.parametrize(
    "weight,unit,problem",
    [(-999, "kg", "missing_weight"),
     ("8.16", "kg", "implausible_weight"),
     ("1800", "kg", "implausible_weight"),
     ("81.6", None, "missing_weight_unit"),
     ("", "kg", "missing_weight")],
)
def test_unusable_weights_block_the_dose_instead_of_computing_one(weight, unit, problem):
    row = vitals("VT-1", "S-100", "2025-06-01", weight, unit)
    if unit is None:
        row.pop("weight_unit")
    result = normalise_dose(Decimal("5"), "mg/kg", "mg", "S-100",
                            date(2025, 6, 1), source=weights(row))
    assert not result.ok
    assert result.code == "weight_unusable"
    assert result.detail["problem"] == problem


def test_an_unusable_weight_is_not_silently_replaced_by_an_earlier_one():
    """The Week 4 weight is not the Week 8 weight. Reaching back would be an
    undeclared imputation, and it would hide the data quality problem."""
    source = weights(
        vitals("VT-1", "S-100", "2025-03-01", "80.0"),
        vitals("VT-2", "S-100", "2025-06-01", -999),
    )
    result = normalise_dose(Decimal("5"), "mg/kg", "mg", "S-100",
                            date(2025, 6, 1), source=source)
    assert not result.ok
    assert "undeclared imputation" in result.explanation


def test_an_imprecise_measurement_date_cannot_anchor_an_as_of_lookup():
    source = weights(vitals("VT-1", "S-100", "2025-06", "80.0"))
    result = normalise_dose(Decimal("5"), "mg/kg", "mg", "S-100",
                            date(2025, 6, 15), source=source)
    assert not result.ok
    assert result.code == "no_weight_on_or_before"


def test_an_unattributable_subject_is_refused():
    result = normalise_dose(Decimal("5"), "mg/kg", "mg", "", date(2025, 6, 1),
                            source=weights())
    assert not result.ok
    assert result.code == "unattributable_subject"


def test_a_missing_unit_argument_is_an_error_not_an_assumed_kg():
    result = normalise_dose(Decimal("5"), None, "mg", "S-100", date(2025, 6, 1),
                            source=weights())
    assert not result.ok
    assert result.code == "unknown_unit"


def test_a_missing_value_is_not_treated_as_zero():
    result = normalise_dose("NA", "mg/kg", "mg", "S-100", date(2025, 6, 1),
                            source=weights())
    assert not result.ok
    assert result.code == "missing_value"


def test_nothing_raises_into_the_caller():
    """Every failure path returns a structured result."""
    for value, from_unit in [("banana", "mg/kg"), (None, "mg/kg"), (5, "furlongs")]:
        result = normalise_dose(value, from_unit, "mg", "S-100",
                                date(2025, 6, 1), source=weights())
        assert result.ok is False
        assert result.to_dict()["ok"] is False


# --------------------------------------------------------------------------
# Rounding once, at the boundary
# --------------------------------------------------------------------------

def test_rounding_happens_once_at_the_end():
    """Quantizing the kilogram intermediate first would give 408 mg from a
    different, wrong path -- and 407 mg for weights that round the other way."""
    source = weights(vitals("VT-1", "S-100", "2025-06-01", "180", "lb"))
    result = normalise_dose(Decimal("5"), "mg/kg", "mg", "S-100",
                            date(2025, 6, 1), source=source)
    assert result.weight_used == Decimal("81.6466266")   # not 81.6
    assert result.dose == Decimal("408")                  # 408.233133 -> 408


def test_places_argument_controls_the_single_rounding_step():
    source = weights(vitals("VT-1", "S-100", "2025-06-01", "180", "lb"))
    result = normalise_dose(Decimal("5"), "mg/kg", "mg", "S-100",
                            date(2025, 6, 1), source=source, places=3)
    assert result.dose == Decimal("408.233")


# --------------------------------------------------------------------------
# Other conversions
# --------------------------------------------------------------------------

def test_mass_to_mass_needs_no_weight_lookup():
    result = normalise_dose(Decimal("408"), "mg", "mcg", "S-100",
                            date(2025, 6, 1), source=weights(), places=0)
    assert result.ok
    assert result.dose == Decimal("408000")
    assert result.weight_used is None
    assert result.method == "mass_conversion"


def test_mg_back_to_mg_per_kg():
    source = weights(vitals("VT-1", "S-100", "2025-06-01", "80.0"))
    result = normalise_dose(Decimal("400"), "mg", "mg/kg", "S-100",
                            date(2025, 6, 1), source=source, places=2)
    assert result.dose == Decimal("5.00")


def test_mg_per_m2_uses_mosteller_and_shows_the_bsa():
    source = weights(vitals("VT-1", "S-100", "2025-06-01", "80.0", "kg", "180"))
    result = normalise_dose(Decimal("100"), "mg/m2", "mg", "S-100",
                            date(2025, 6, 1), source=source)
    assert result.ok
    assert result.dose == Decimal("200")           # BSA = 2.0 m2
    assert result.method == "mg_per_m2_mosteller"
    assert "Mosteller" in result.explanation


def test_mg_per_m2_without_a_height_is_a_structured_error():
    row = vitals("VT-1", "S-100", "2025-06-01", "80.0", "kg", "NA")
    result = normalise_dose(Decimal("100"), "mg/m2", "mg", "S-100",
                            date(2025, 6, 1), source=weights(row))
    assert not result.ok
    assert result.code == "missing_height"


# --------------------------------------------------------------------------
# Against the real dataset
# --------------------------------------------------------------------------

def test_clean_subject_doses_are_computable_across_the_whole_study(real_weights):
    """S-015 is a control-group subject: every dosing visit must resolve."""
    import json
    dosing = json.loads((ROOT / "data" / "dosing.json").read_text(encoding="utf-8"))
    rows = [r for r in dosing if r["subject_id"] == "S-015"]
    assert rows
    for row in rows:
        result = normalise_dose(
            Decimal("5"), "mg/kg", "mg", "S-015",
            date.fromisoformat(row["dose_date"]), source=real_weights,
        )
        assert result.ok, result.explanation
        assert result.exact_date_match is True


def test_site_03_damaged_weights_are_reported_not_computed(real_weights):
    """S-013's Week 8 weight is -999 in the real dataset."""
    result = normalise_dose(Decimal("5"), "mg/kg", "mg", "S-013",
                            date(2025, 7, 28), source=real_weights)
    assert not result.ok
