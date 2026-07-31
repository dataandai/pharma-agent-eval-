"""Phase 1 gate: the generated dataset matches docs/DATA_TRAPS.md.

These are not detector tests -- no detector exists yet. They pin the ground
truth so that a later change to the generator cannot silently move a trap out
from under the detectors that will be written against it.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
CLEAN_SUBJECTS = {"S-009", "S-012", "S-014", "S-015"}
SENTINELS = {"", "NA", "N/A", ".", "UNK", "Not Done", "ND", -999}
S007_ALIASES = {"S-007", "S007", "007", "SITE03-007", "s-007"}


def load(name: str):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def dataset():
    return {
        "protocol": load("protocol.json"),
        "subjects": {s["subject_id"]: s for s in load("subjects.json")},
        "visits": load("visits.json"),
        "dosing": load("dosing.json"),
        "vitals": load("vitals.json"),
        "deviation_log": load("deviation_log.json"),
    }


def visits_for(dataset, subject_id, visit_id=None):
    aliases = S007_ALIASES if subject_id == "S-007" else {subject_id}
    return [
        r for r in dataset["visits"]
        if r["subject_id"] in aliases and (visit_id is None or r["visit_id"] == visit_id)
    ]


def window_for(dataset, subject_id, visit_id):
    version = dataset["subjects"][subject_id]["protocol_version_consented"]
    schedule = next(p for p in dataset["protocol"] if p["version"] == version)["visit_schedule"]
    return next(v for v in schedule if v["visit_id"] == visit_id)


def target_for(dataset, subject_id, visit_id):
    anchor = date.fromisoformat(dataset["subjects"][subject_id]["anchor_date"])
    visit = window_for(dataset, subject_id, visit_id)
    return anchor + timedelta(days=visit["target_day"] - 1)


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------

def test_generation_is_deterministic():
    """A seeded generator that drifts between runs makes every downstream
    assertion meaningless."""
    names = ["protocol.json", "subjects.json", "visits.json",
             "dosing.json", "vitals.json", "deviation_log.json"]

    def digest():
        return {n: hashlib.sha256((DATA / n).read_bytes()).hexdigest() for n in names}

    before = digest()
    subprocess.run([sys.executable, str(ROOT / "scripts" / "generate_data.py")],
                   cwd=ROOT, check=True, capture_output=True)
    assert digest() == before


# --------------------------------------------------------------------------
# Layer B — domain traps
# --------------------------------------------------------------------------

def test_b1_protocol_states_a_total_not_a_per_dose_amount(dataset):
    dosing = dataset["protocol"][0]["dosing"]
    assert dosing["total_doses"] == 8
    assert dosing["treatment_period_weeks"] == 32
    assert dosing["value"] == "5" and dosing["unit"] == "mg/kg"
    # The per-administration expectation must not be readable from a field.
    for forbidden in ("dose_mg", "dose_per_cycle", "interval_days", "expected_dose"):
        assert forbidden not in dosing


def test_b2_version_lineage_same_lateness_opposite_verdict(dataset):
    """S-004 and S-009 are late by the same number of days and must reach
    opposite verdicts, because they are governed by different versions."""
    lateness = {}
    for subject_id in ("S-004", "S-009"):
        actual = date.fromisoformat(visits_for(dataset, subject_id, "V2")[0]["visit_date"])
        lateness[subject_id] = (actual - target_for(dataset, subject_id, "V2")).days

    assert lateness["S-004"] == lateness["S-009"] == 4

    s004 = window_for(dataset, "S-004", "V2")
    s009 = window_for(dataset, "S-009", "V2")
    assert dataset["subjects"]["S-004"]["protocol_version_consented"] == "1.0"
    assert dataset["subjects"]["S-009"]["protocol_version_consented"] == "2.0"
    assert s004["window_after"] == 3 and s009["window_after"] == 5

    assert lateness["S-004"] > s004["window_after"]   # deviation
    assert lateness["S-009"] <= s009["window_after"]  # compliant


def test_b2_s004_consented_under_v1_but_visited_after_v2_took_effect(dataset):
    """The trap only bites if the calendar date really is in v2.0 territory."""
    v2_effective = date.fromisoformat(
        next(p for p in dataset["protocol"] if p["version"] == "2.0")["effective_date"]
    )
    consent = date.fromisoformat(dataset["subjects"]["S-004"]["consent_date"])
    visit = date.fromisoformat(visits_for(dataset, "S-004", "V2")[0]["visit_date"])
    assert consent < v2_effective <= visit


def test_b3_screen_failure_was_never_dosed(dataset):
    assert dataset["subjects"]["S-011"]["anchor_date"] is None
    assert dataset["subjects"]["S-011"]["screen_failure_reason"]
    assert visits_for(dataset, "S-011") and all(
        r["visit_id"] == "SCR" for r in visits_for(dataset, "S-011")
    )
    assert [r for r in dataset["dosing"] if r["subject_id"] == "S-011"] == []


def test_b4_already_logged_deviation_is_present_in_the_log(dataset):
    logged = dataset["deviation_log"]
    assert len(logged) == 1
    entry = logged[0]
    assert entry["subject_id"] == "S-002" and entry["visit_id"] == "V3"
    actual = date.fromisoformat(visits_for(dataset, "S-002", "V3")[0]["visit_date"])
    assert entry["deviation_date"] == actual.isoformat()
    # And it is genuinely out of window, so suppression is not hiding a non-event.
    assert (actual - target_for(dataset, "S-002", "V3")).days == 6


def test_b5_orphan_visit_has_no_subject(dataset):
    orphans = [r for r in dataset["visits"] if r["subject_id"] == ""]
    assert len(orphans) == 1


def test_b6_lb_weight_resolves_to_408_mg(dataset):
    vitals = [r for r in dataset["vitals"]
              if r["subject_id"] in S007_ALIASES and r["visit_id"] == "V1"][0]
    assert vitals["weight_unit"] == "lb"
    kg = Decimal(str(vitals["weight"])) * Decimal("0.45359237")
    assert (Decimal("5") * kg).quantize(Decimal("1")) == Decimal("408")

    dose = [r for r in dataset["dosing"]
            if r["subject_id"] in S007_ALIASES and r["visit_id"] == "V1"][0]
    assert Decimal(str(dose["dose_administered"])) == Decimal("408")
    # Read as kg the same record looks like a >50% underdose.
    as_kg = Decimal("5") * Decimal(str(vitals["weight"]))
    assert (as_kg - Decimal("408")) / as_kg > Decimal("0.5")


def test_b7_per_visit_windows_differ(dataset):
    schedule = dataset["protocol"][0]["visit_schedule"]
    windows = {v["visit_id"]: v["window_after"] for v in schedule if v["target_day"]}
    assert windows["V1"] == 0
    assert windows["V3"] == 3
    assert windows["V5"] == 7
    assert windows["V6"] == 14


def test_b8_withheld_dose_is_below_the_protocol_hold_threshold(dataset):
    """The dose was withheld at ANC 0.8; the protocol only permits holding
    below 0.5. That gap is what makes it a deviation rather than per-protocol,
    and E6(R3) 2.5.3 is what makes it CAPA-exempt."""
    dose = [r for r in dataset["dosing"]
            if r["subject_id"] == "S-005" and r["visit_id"] == "V3"][0]
    assert dose["dose_status"] == "Withheld"
    assert dose["dose_administered"] is None
    assert "0.8" in dose["reason"] and "neutropenia" in dose["reason"].lower()
    assert "ANC < 0.5 x10^9/L" in dataset["protocol"][0]["dosing"]["hold_criteria"]


def test_b9_systemic_pattern_is_four_of_five_at_site_02(dataset):
    site02 = [s for s in dataset["subjects"].values()
              if s["site_id"] == "SITE-02" and s["anchor_date"]]
    assert len(site02) == 5

    deviations = []
    for subject in site02:
        subject_id = subject["subject_id"]
        records = visits_for(dataset, subject_id, "V2")
        actual = date.fromisoformat(records[0]["visit_date"])
        delta = (actual - target_for(dataset, subject_id, "V2")).days
        if abs(delta) > window_for(dataset, subject_id, "V2")["window_after"]:
            deviations.append(subject_id)

    assert len(deviations) == 4
    assert all(4 <= abs(
        (date.fromisoformat(visits_for(dataset, s, "V2")[0]["visit_date"])
         - target_for(dataset, s, "V2")).days) <= 5 for s in deviations)


def test_b10_screening_procedure_predates_consent(dataset):
    consent = date.fromisoformat(dataset["subjects"]["S-006"]["consent_date"])
    screening = date.fromisoformat(visits_for(dataset, "S-006", "SCR")[0]["visit_date"])
    assert screening < consent
    assert (consent - screening).days == 1


def test_b11_missing_ecg_only_matters_under_v2(dataset):
    assert dataset["subjects"]["S-003"]["protocol_version_consented"] == "2.0"
    required = window_for(dataset, "S-003", "V4")["required_assessments"]
    assert "ecg" in required
    done = visits_for(dataset, "S-003", "V4")[0]["assessments_done"]
    assert "ecg" not in done
    # A v1.0 subject with the same record would be compliant.
    assert "ecg" not in window_for(dataset, "S-004", "V4")["required_assessments"]


def test_b12_missed_visit_is_absent_with_a_closed_window(dataset):
    assert visits_for(dataset, "S-001", "V3") == []
    closes = target_for(dataset, "S-001", "V3") + timedelta(days=3)
    assert closes < date.today()


# --------------------------------------------------------------------------
# Layer A — the control group and the assessability band
# --------------------------------------------------------------------------

@pytest.mark.parametrize("subject_id", sorted(CLEAN_SUBJECTS))
def test_clean_subjects_carry_no_layer_a_damage(dataset, subject_id):
    """A dataset where everything is broken proves as little as one where
    nothing is."""
    records = (
        [r for r in dataset["visits"] if r["subject_id"] == subject_id]
        + [r for r in dataset["vitals"] if r["subject_id"] == subject_id]
        + [r for r in dataset["dosing"] if r["subject_id"] == subject_id]
        + [dataset["subjects"][subject_id]]
    )
    assert records
    for record in records:
        for field, value in record.items():
            if field.endswith("date") and isinstance(value, str):
                # Must be a full, unambiguous ISO date. '05/06/2025' is also ten
                # characters long, so length alone is not the check.
                date.fromisoformat(value)
            if isinstance(value, (str, int, float)) and field not in ("height_cm",):
                assert value not in SENTINELS, f"{subject_id} {field}={value!r} is a sentinel"
    for record in dataset["vitals"]:
        if record["subject_id"] == subject_id:
            assert record.get("weight_unit") == "kg"


def test_clean_subjects_are_actually_compliant(dataset):
    for subject_id in sorted(CLEAN_SUBJECTS):
        for record in [r for r in dataset["visits"] if r["subject_id"] == subject_id]:
            if record["visit_id"] == "SCR":
                continue
            visit = window_for(dataset, subject_id, record["visit_id"])
            delta = (date.fromisoformat(record["visit_date"])
                     - target_for(dataset, subject_id, record["visit_id"])).days
            assert -visit["window_before"] <= delta <= visit["window_after"], \
                f"{subject_id} {record['visit_id']} is {delta} days out"
            assert set(visit["required_assessments"]) <= set(record["assessments_done"])


def test_not_assessable_rate_is_inside_the_band():
    """Below 5% the messiness is decorative; above 25% the study looks
    abandoned. The generator prints the figure it achieved."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "generate_data.py")],
        cwd=ROOT, check=True, capture_output=True, text=True,
    )
    line = next(l for l in result.stdout.splitlines() if "expected not_assessable" in l)
    rate = float(line.split("(")[1].split("%")[0])
    assert 5.0 <= rate <= 25.0
    assert "OUT OF BAND" not in result.stdout


def test_site_03_dominates_the_unassessable_records():
    """The site-level quality pattern is itself a finding, and it must be
    visible in the data rather than asserted in prose."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "generate_data.py")],
        cwd=ROOT, check=True, capture_output=True, text=True,
    )
    line = next(l for l in result.stdout.splitlines() if l.strip().startswith("SITE-03"))
    share = float(line.split("(")[1].split("%")[0])
    assert share >= 55.0


def test_subject_id_drift_is_confined_to_site_03(dataset):
    """Other sites are consistent; only SITE-03 needs normalisation."""
    known = set(dataset["subjects"])
    drifted = {
        r["subject_id"] for r in dataset["visits"] + dataset["dosing"] + dataset["vitals"]
        if r["subject_id"] not in known and r["subject_id"] not in ("", "S-099")
    }
    assert drifted
    assert drifted <= S007_ALIASES


def test_the_registry_verification_catches_a_fabricated_claim():
    """The ground truth is rendered from the registry the data is built from, so
    it cannot disagree with the generator. This pass re-reads the written files
    and at least stops the registry naming something that is not there."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "gen", ROOT / "scripts" / "generate_data.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)

    assert gen.verify_registry(DATA) == []      # nothing planted yet

    gen.ISSUES.append({
        "layer": "B", "code": "FAKE", "file": "visits.json",
        "records": ["VR-9999"], "subject": "S-999", "verdict": "deviation",
        "routing": "-", "note": "-",
        "expect": [gen.expectation("S-999", "V2", "deviation")],
    })
    problems = gen.verify_registry(DATA)
    assert any("VR-9999" in p for p in problems)
    assert any("S-999" in p for p in problems)
