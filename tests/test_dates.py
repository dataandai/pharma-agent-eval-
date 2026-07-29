from datetime import date

import pytest

from src.dates import Precision, days_between, parse_clinical_date
from src.verdicts import Verdict, combine


# --------------------------------------------------------------------------
# Parsing the forms the dataset contains
# --------------------------------------------------------------------------

def test_a_full_iso_date_is_exact():
    parsed = parse_clinical_date("2025-06-15")
    assert parsed.precision is Precision.DAY
    assert parsed.exact == date(2025, 6, 15)
    assert parsed.earliest == parsed.latest == date(2025, 6, 15)


@pytest.mark.parametrize("raw", ["2025-06", "2025-06-UN", "2025-06-UNK", "2025-06-XX"])
def test_month_precision_becomes_the_whole_month(raw):
    """A partial date is not a broken date -- it is an interval."""
    parsed = parse_clinical_date(raw)
    assert parsed.precision is Precision.MONTH
    assert parsed.earliest == date(2025, 6, 1)
    assert parsed.latest == date(2025, 6, 30)
    assert parsed.exact is None


def test_february_month_span_respects_leap_years():
    assert parse_clinical_date("2024-02").latest == date(2024, 2, 29)
    assert parse_clinical_date("2025-02").latest == date(2025, 2, 28)


@pytest.mark.parametrize("raw", ["2025", "2025-UN", "2025-UN-UN"])
def test_year_precision_becomes_the_whole_year(raw):
    parsed = parse_clinical_date(raw)
    assert parsed.precision is Precision.YEAR
    assert parsed.earliest == date(2025, 1, 1)
    assert parsed.latest == date(2025, 12, 31)


@pytest.mark.parametrize("raw", ["", None, "NA", "N/A", "UNK", ".", -999, "Not Done"])
def test_missing_values_are_unknown_not_guessed(raw):
    parsed = parse_clinical_date(raw)
    assert parsed.precision is Precision.UNKNOWN
    assert parsed.known is False
    assert parsed.candidates == ()


def test_an_impossible_date_is_unknown_rather_than_shifted():
    parsed = parse_clinical_date("2025-02-30")
    assert parsed.known is False
    assert "not a real calendar date" in parsed.note


def test_nothing_is_ever_imputed():
    """CDISC: always reflect what is known; never infer missing data without
    justification. A month-precision date must not silently become the 1st or
    the 15th."""
    parsed = parse_clinical_date("2025-06")
    assert parsed.exact is None
    assert parsed.is_exact is False


# --------------------------------------------------------------------------
# Slashed dates
# --------------------------------------------------------------------------

def test_a_slashed_date_with_two_valid_readings_is_ambiguous():
    parsed = parse_clinical_date("05/06/2025")
    assert parsed.precision is Precision.AMBIGUOUS
    assert {span[0] for span in parsed.candidates} == {date(2025, 6, 5), date(2025, 5, 6)}
    assert "DD/MM/YYYY" in parsed.note and "MM/DD/YYYY" in parsed.note


def test_a_slashed_date_with_only_one_valid_reading_is_exact():
    """25 is not a month, so '25/06/2025' can only be 25 June."""
    parsed = parse_clinical_date("25/06/2025")
    assert parsed.precision is Precision.DAY
    assert parsed.exact == date(2025, 6, 25)


def test_a_slashed_date_where_both_readings_coincide_is_exact():
    parsed = parse_clinical_date("05/05/2025")
    assert parsed.precision is Precision.DAY
    assert parsed.exact == date(2025, 5, 5)


def test_a_slashed_date_with_no_valid_reading_is_unknown():
    assert parse_clinical_date("31/31/2025").known is False


# --------------------------------------------------------------------------
# Window arithmetic -- where the three verdicts come from
# --------------------------------------------------------------------------

def test_an_exact_date_inside_the_window():
    parsed = parse_clinical_date("2025-06-15")
    inside, outside = parsed.possible_days_within(date(2025, 6, 12), date(2025, 6, 18))
    assert (inside, outside) == (True, False)


def test_an_exact_date_outside_the_window():
    parsed = parse_clinical_date("2025-06-25")
    inside, outside = parsed.possible_days_within(date(2025, 6, 12), date(2025, 6, 18))
    assert (inside, outside) == (False, True)


def test_a_month_entirely_outside_the_window_is_decidable():
    """The verdict does not depend on the missing day."""
    parsed = parse_clinical_date("2025-10")
    inside, outside = parsed.possible_days_within(date(2025, 8, 22), date(2025, 8, 28))
    assert (inside, outside) == (False, True)


def test_a_month_straddling_the_window_is_not_decidable():
    parsed = parse_clinical_date("2025-08")
    inside, outside = parsed.possible_days_within(date(2025, 8, 22), date(2025, 8, 28))
    assert (inside, outside) == (True, True)


def test_a_window_wholly_inside_the_recorded_month_is_not_decidable():
    """A +/-3 day window sits inside any month, so a month-precision date can
    never confirm compliance -- only refute it."""
    parsed = parse_clinical_date("2025-08")
    inside, outside = parsed.possible_days_within(date(2025, 8, 1), date(2025, 8, 31))
    assert inside is True and outside is False


# --------------------------------------------------------------------------
# Ordering
# --------------------------------------------------------------------------

def test_entirely_before_and_after_need_the_whole_range_to_agree():
    june = parse_clinical_date("2025-06")
    assert june.entirely_before(date(2025, 7, 1)) is True
    assert june.entirely_before(date(2025, 6, 15)) is False
    assert june.entirely_after(date(2025, 5, 31)) is True
    assert june.entirely_after(date(2025, 6, 15)) is False


def test_days_between_returns_a_range_for_imprecise_dates():
    consent = parse_clinical_date("2025-06-01")
    visit = parse_clinical_date("2025-06")
    # The visit could be the same day (0) or the last day of the month (29).
    assert days_between(consent, visit) == (0, 29)


def test_days_between_can_be_negative_when_the_order_is_uncertain():
    """A screening lab recorded as 'June 2025' against a consent on the 15th
    could precede consent by up to 14 days or follow it by 15 -- which is
    exactly the consent-sequence question, and it is unanswerable."""
    consent = parse_clinical_date("2025-06-15")
    lab = parse_clinical_date("2025-06")
    low, high = days_between(consent, lab)
    assert low < 0 < high


def test_days_between_collapses_to_one_number_when_both_are_exact():
    low, high = days_between(parse_clinical_date("2025-06-01"),
                             parse_clinical_date("2025-06-15"))
    assert low == high == 14


def test_days_between_is_none_when_either_side_is_unusable():
    assert days_between(parse_clinical_date("NA"), parse_clinical_date("2025-06-15")) is None


# --------------------------------------------------------------------------
# Verdict algebra
# --------------------------------------------------------------------------

def test_not_assessable_dominates():
    """Reporting compliance over data you could not read is the failure mode
    the third verdict exists to prevent."""
    assert combine([Verdict.COMPLIANT, Verdict.NOT_ASSESSABLE]) is Verdict.NOT_ASSESSABLE
    assert combine([Verdict.DEVIATION, Verdict.NOT_ASSESSABLE]) is Verdict.NOT_ASSESSABLE


def test_a_deviation_beats_compliance():
    assert combine([Verdict.COMPLIANT, Verdict.DEVIATION]) is Verdict.DEVIATION


def test_all_compliant_is_compliant():
    assert combine([Verdict.COMPLIANT, Verdict.COMPLIANT]) is Verdict.COMPLIANT


def test_nothing_to_combine_is_not_assessable():
    assert combine([]) is Verdict.NOT_ASSESSABLE


def test_only_a_deviation_is_a_finding():
    assert Verdict.DEVIATION.is_finding is True
    assert Verdict.COMPLIANT.is_finding is False
    assert Verdict.NOT_ASSESSABLE.is_finding is False
