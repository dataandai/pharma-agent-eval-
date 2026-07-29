from datetime import date, timedelta
from pathlib import Path

import pytest

from src.dates import parse_clinical_date
from src.protocol import (
    assess_timing,
    expected_schedule,
    governing_version,
    load_protocol,
    load_subjects,
    normalise_status,
)
from src.verdicts import Verdict

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def catalogue():
    return load_protocol(ROOT / "data" / "protocol.json")


@pytest.fixture(scope="module")
def subjects():
    return load_subjects(ROOT / "data" / "subjects.json")


def visit_date(subject_id, visit_id):
    import json
    rows = json.loads((ROOT / "data" / "visits.json").read_text(encoding="utf-8"))
    aliases = {"S-007", "S007", "007", "SITE03-007", "s-007"} \
        if subject_id == "S-007" else {subject_id}
    found = [r for r in rows if r["subject_id"] in aliases and r["visit_id"] == visit_id]
    return parse_clinical_date(found[0]["visit_date"]) if found else None


# --------------------------------------------------------------------------
# THE pair: same calendar lateness, opposite verdict
# --------------------------------------------------------------------------

def test_s004_and_s009_are_late_by_exactly_the_same_number_of_days(catalogue, subjects):
    offsets = {}
    for subject_id in ("S-004", "S-009"):
        schedule = expected_schedule(subjects[subject_id], catalogue)
        week4 = schedule.visit("V2")
        actual = visit_date(subject_id, "V2")
        offsets[subject_id] = (actual.exact - week4.target_date).days
    assert offsets["S-004"] == offsets["S-009"] == 4


def test_s004_and_s009_reach_opposite_verdicts(catalogue, subjects):
    """The whole point of the version lineage resolver. Identical calendar
    arithmetic, different governing version, opposite answers."""
    verdicts = {}
    for subject_id in ("S-004", "S-009"):
        schedule = expected_schedule(subjects[subject_id], catalogue)
        assessment = assess_timing(schedule.visit("V2"), visit_date(subject_id, "V2"))
        verdicts[subject_id] = assessment.verdict

    assert verdicts["S-004"] is Verdict.DEVIATION
    assert verdicts["S-009"] is Verdict.COMPLIANT


def test_the_difference_is_the_window_not_the_dates(catalogue, subjects):
    windows = {
        subject_id: expected_schedule(subjects[subject_id], catalogue).visit("V2")
        for subject_id in ("S-004", "S-009")
    }
    assert windows["S-004"].window_after == 3
    assert windows["S-009"].window_after == 5


def test_s004_is_governed_by_v1_although_v2_was_in_force_at_the_visit(catalogue, subjects):
    """S-004 consented eleven days before v2.0 took effect and their Week 4
    visit lands in August, when v2.0 is live. They keep v1.0."""
    when = visit_date("S-004", "V2").exact
    resolved = governing_version(subjects["S-004"], catalogue, on_date=when)

    assert resolved.version.version == "1.0"
    assert resolved.version_in_force.version == "2.0"
    assert resolved.differs_from_calendar_version is True
    assert "does not govern a subject who consented earlier" in resolved.explanation


def test_measuring_s004_against_v2_would_suppress_a_real_deviation(catalogue, subjects):
    """Documents the size of the error the resolver prevents."""
    schedule = expected_schedule(subjects["S-004"], catalogue)
    actual = visit_date("S-004", "V2")
    assert assess_timing(schedule.visit("V2"), actual).verdict is Verdict.DEVIATION

    wrong_window = catalogue.version("2.0").visit("V2")
    target = schedule.visit("V2").target_date
    late = (actual.exact - target).days
    assert late <= wrong_window["window_after"]   # would have looked compliant


def test_measuring_s009_against_v1_would_fabricate_a_deviation(catalogue, subjects):
    schedule = expected_schedule(subjects["S-009"], catalogue)
    actual = visit_date("S-009", "V2")
    assert assess_timing(schedule.visit("V2"), actual).verdict is Verdict.COMPLIANT

    wrong_window = catalogue.version("1.0").visit("V2")
    late = (actual.exact - schedule.visit("V2").target_date).days
    assert late > wrong_window["window_after"]    # would have looked like a deviation


def test_governing_version_ignores_on_date_entirely(catalogue, subjects):
    """on_date reports what a calendar lookup would have said; it never selects
    the version."""
    subject = subjects["S-004"]
    picks = {
        governing_version(subject, catalogue, on_date=day).version.version
        for day in (date(2025, 1, 20), date(2025, 6, 30), date(2025, 7, 2),
                    date(2026, 1, 1), None)
    }
    assert picks == {"1.0"}


# --------------------------------------------------------------------------
# Version lineage
# --------------------------------------------------------------------------

def test_amendment_lineage_chains_back_to_the_original(catalogue):
    chain = catalogue.lineage("2.0")
    assert [v.version for v in chain] == ["1.0", "2.0"]
    assert catalogue.version("2.0").amends == "1.0"
    assert catalogue.version("1.0").amends is None


def test_version_in_force_is_a_calendar_lookup(catalogue):
    assert catalogue.version_in_force_on(date(2025, 1, 14)) is None
    assert catalogue.version_in_force_on(date(2025, 1, 15)).version == "1.0"
    assert catalogue.version_in_force_on(date(2025, 6, 30)).version == "1.0"
    assert catalogue.version_in_force_on(date(2025, 7, 1)).version == "2.0"


def test_the_amendment_widened_week4_and_added_an_ecg(catalogue):
    v1, v2 = catalogue.version("1.0"), catalogue.version("2.0")
    assert v1.visit("V2")["window_after"] == 3
    assert v2.visit("V2")["window_after"] == 5
    assert "ecg" not in v1.required_assessments("V4")
    assert "ecg" in v2.required_assessments("V4")


# --------------------------------------------------------------------------
# Per-subject anchors
# --------------------------------------------------------------------------

def test_windows_are_computed_from_each_subjects_own_anchor(catalogue, subjects):
    """Day 1 is a different calendar date for every subject, so 'Day 29 +/-3'
    is a different window for every subject."""
    targets = {}
    for subject_id in ("S-001", "S-005", "S-012"):
        schedule = expected_schedule(subjects[subject_id], catalogue)
        targets[subject_id] = schedule.visit("V2").target_date
        assert schedule.visit("V1").target_date == subjects[subject_id].anchor_date
    assert len(set(targets.values())) == 3


def test_day_n_is_anchor_plus_n_minus_one(catalogue, subjects):
    """There is no day 0. Getting this wrong shifts every window in the study."""
    schedule = expected_schedule(subjects["S-001"], catalogue)
    anchor = subjects["S-001"].anchor_date
    assert schedule.visit("V1").target_date == anchor
    assert schedule.visit("V2").target_date == anchor + timedelta(days=28)
    assert schedule.visit("V3").target_date == anchor + timedelta(days=56)
    assert schedule.visit("V5").target_date == anchor + timedelta(days=168)


def test_per_visit_windows_are_not_uniform(catalogue, subjects):
    """A single global tolerance gives wrong answers at both ends."""
    schedule = expected_schedule(subjects["S-001"], catalogue)
    spans = {v.visit_id: (v.closes - v.opens).days for v in schedule.visits
             if not v.is_screening}
    assert spans["V1"] == 0      # Day 1 is exact
    assert spans["V3"] == 6      # +/-3
    assert spans["V5"] == 14     # +/-7
    assert spans["V6"] == 28     # +/-14


def test_screening_window_sits_before_day_one(catalogue, subjects):
    schedule = expected_schedule(subjects["S-001"], catalogue)
    screening = schedule.visit("SCR")
    anchor = subjects["S-001"].anchor_date
    assert screening.is_screening
    assert screening.opens == anchor - timedelta(days=28)
    assert screening.closes == anchor - timedelta(days=1)


def test_a_screen_failure_has_no_schedule_at_all(catalogue, subjects):
    """Protocol compliance does not apply to a subject who was never enrolled."""
    subject = subjects["S-011"]
    assert subject.is_screen_failure
    assert subject.is_enrolled is False
    schedule = expected_schedule(subject, catalogue)
    assert schedule.visits == ()
    assert schedule.anchored is False
    assert "never enrolled" in schedule.note


# --------------------------------------------------------------------------
# Three-valued window assessment
# --------------------------------------------------------------------------

def test_an_exact_date_inside_the_window_is_compliant(catalogue, subjects):
    schedule = expected_schedule(subjects["S-014"], catalogue)
    week4 = schedule.visit("V2")
    assessment = assess_timing(week4, parse_clinical_date(week4.target_date.isoformat()))
    assert assessment.verdict is Verdict.COMPLIANT
    assert assessment.days_late == 0


def test_a_month_precision_date_whose_whole_month_is_outside_is_still_a_deviation(
    catalogue, subjects
):
    """S-013's Week 12 is recorded as a month, and the visit was six weeks late.
    Refusing to answer here would be as wrong as imputing a day."""
    schedule = expected_schedule(subjects["S-013"], catalogue)
    assessment = assess_timing(schedule.visit("V4"), visit_date("S-013", "V4"))
    assert assessment.verdict is Verdict.DEVIATION
    assert "does not depend on the missing detail" in assessment.calculation


def test_a_month_precision_date_straddling_the_window_is_not_assessable(
    catalogue, subjects
):
    schedule = expected_schedule(subjects["S-001"], catalogue)
    week4 = schedule.visit("V2")
    straddling = parse_clinical_date(week4.target_date.strftime("%Y-%m"))
    assessment = assess_timing(week4, straddling)
    assert assessment.verdict is Verdict.NOT_ASSESSABLE
    assert "would depend on information the record does not carry" in assessment.calculation


def test_an_ambiguous_slashed_date_straddling_the_window_is_not_assessable(
    catalogue, subjects
):
    """S-008's Week 24 is written '01/10/2025'. Read DD/MM it is inside the
    window; read MM/DD it is nine months earlier and outside. Picking a locale
    and proceeding is how a confident wrong answer gets produced."""
    actual = visit_date("S-008", "V5")
    assert actual.precision.value == "ambiguous"
    schedule = expected_schedule(subjects["S-008"], catalogue)
    assessment = assess_timing(schedule.visit("V5"), actual)
    assert assessment.verdict is Verdict.NOT_ASSESSABLE


def test_an_ambiguous_date_is_still_decidable_when_both_readings_agree(
    catalogue, subjects
):
    """Ambiguity is not automatically unassessable. If every reading falls
    outside the window the verdict does not depend on resolving it -- the same
    principle as the month-precision case, and refusing to answer would be a
    false negative dressed up as caution."""
    schedule = expected_schedule(subjects["S-001"], catalogue)
    week4 = schedule.visit("V2")
    # A date far from the window under either reading.
    far = parse_clinical_date("11/12/2026")
    assert far.precision.value == "ambiguous"
    assert assess_timing(week4, far).verdict is Verdict.DEVIATION


def test_an_unreadable_date_is_not_assessable(catalogue, subjects):
    schedule = expected_schedule(subjects["S-001"], catalogue)
    assessment = assess_timing(schedule.visit("V2"), parse_clinical_date("NA"))
    assert assessment.verdict is Verdict.NOT_ASSESSABLE
    assert assessment.offset_days is None


def test_the_calculation_string_carries_the_real_window_and_dates(catalogue, subjects):
    """The agent reads this out; the model never produces a figure."""
    schedule = expected_schedule(subjects["S-004"], catalogue)
    week4 = schedule.visit("V2")
    assessment = assess_timing(week4, visit_date("S-004", "V2"))
    assert week4.target_date.isoformat() in assessment.calculation
    assert week4.opens.isoformat() in assessment.calculation
    assert week4.closes.isoformat() in assessment.calculation
    assert "4 days late" in assessment.calculation


# --------------------------------------------------------------------------
# Layer A: enrolment status drift
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw", ["Randomized", "randomised", "RAND ", "Rand", " randomized "],
)
def test_enrolment_status_spellings_collapse(raw):
    assert normalise_status(raw) == "randomized"


def test_every_subject_in_the_dataset_has_a_recognised_status(subjects):
    allowed = {"randomized", "completed", "screen_failure", "withdrawn"}
    assert {s.enrollment_status for s in subjects.values()} <= allowed


def test_subject_ids_are_normalised_on_load(subjects):
    assert "S-007" in subjects
    assert len(subjects) == 15
