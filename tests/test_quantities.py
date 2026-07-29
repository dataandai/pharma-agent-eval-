from decimal import Decimal

import pytest

from src.quantities import (
    IncompatibleUnits,
    Quantity,
    UnknownUnit,
    Unparseable,
    bsa_mosteller,
    canonical_unit,
    is_missing,
    parse_decimal,
    parse_quantity,
)


# --------------------------------------------------------------------------
# Missing-value sentinels
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw", ["", None, "NA", "N/A", ".", "UNK", -999, "-999", "Not Done", "ND",
            "  na  ", "nd", "null"],
)
def test_every_sentinel_spelling_means_missing(raw):
    """All of these arrive mixed within one field and all mean the same thing."""
    assert is_missing(raw) is True


@pytest.mark.parametrize("raw", [0, "0", Decimal("0"), 81.6, "81.6", "0.0", False])
def test_real_values_are_not_missing(raw):
    assert is_missing(raw) is False


def test_minus_999_never_reaches_arithmetic():
    """A -999 weight silently entering a mg/kg calculation is the realistic
    version of a catastrophic bug: it yields an expected dose of -4995 mg."""
    assert parse_decimal(-999) is None
    assert parse_decimal("-999") is None


def test_missing_and_unparseable_are_different_problems():
    assert parse_decimal("NA") is None
    with pytest.raises(Unparseable):
        parse_decimal("about eighty")


# --------------------------------------------------------------------------
# Units
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [("mg", "mg"), ("MG", "mg"), (" mg ", "mg"), ("mgs", "mg"), ("milligrams", "mg"),
     ("lb", "lb"), ("LBS", "lb"), ("pounds", "lb"), ("kg", "kg"), ("Kilograms", "kg"),
     ("mcg", "mcg"), ("ug", "mcg"), ("µg", "mcg"),
     ("mg/kg", "mg/kg"), ("MG/KG", "mg/kg"), ("mg per kg", "mg/kg"),
     ("mg/m2", "mg/m2"), ("mg/m²", "mg/m2"), ("mg/m^2", "mg/m2")],
)
def test_unit_spellings_normalise(raw, expected):
    assert canonical_unit(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "  ", "widgets", "IU"])
def test_a_missing_or_unknown_unit_is_never_defaulted(raw):
    """Defaulting to kg is exactly the assumption the pound-recording site
    exists to punish."""
    with pytest.raises(UnknownUnit):
        canonical_unit(raw)


# --------------------------------------------------------------------------
# Conversion
# --------------------------------------------------------------------------

def test_pound_to_kilogram_is_exact():
    """1 lb = 0.45359237 kg by definition. No float, no drift."""
    assert Quantity(Decimal("180"), "lb").convert_to("kg").value == Decimal("81.6466266")


def test_mass_conversions_round_trip_without_loss():
    original = Quantity(Decimal("408"), "mg")
    assert original.convert_to("g").convert_to("mcg").convert_to("mg").value == Decimal("408")


def test_conversion_does_not_round():
    """Rounding an intermediate is how a correct dose becomes a deviation."""
    converted = Quantity(Decimal("180"), "lb").convert_to("kg")
    assert converted.value != converted.value.quantize(Decimal("1"))
    assert converted.quantize(1).value == Decimal("81.6")


def test_quantize_is_half_up():
    assert Quantity(Decimal("408.5"), "mg").quantize(0).value == Decimal("409")
    assert Quantity(Decimal("407.4"), "mg").quantize(0).value == Decimal("407")


def test_incompatible_dimensions_are_refused():
    with pytest.raises(IncompatibleUnits):
        Quantity(Decimal("5"), "mg/kg").convert_to("mg")


# --------------------------------------------------------------------------
# Parsing the encodings the dataset actually uses
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw_value,raw_unit",
    [("408 mg", None), ("408", "mg"), ("408.0", "mg"), (408, "mg"),
     (408.0, "mg"), (Decimal("408"), "mg"), ("408mg", None), ("408 MG", None)],
)
def test_every_encoding_of_the_same_dose_parses_identically(raw_value, raw_unit):
    quantity = parse_quantity(raw_value, raw_unit)
    assert quantity.value == Decimal("408")
    assert quantity.unit == "mg"


def test_embedded_unit_must_agree_with_the_unit_field():
    """The string's unit is not assumed to win; a disagreement is an error."""
    with pytest.raises(IncompatibleUnits):
        parse_quantity("408 mg", "mcg")


def test_value_without_any_unit_is_refused():
    with pytest.raises(UnknownUnit):
        parse_quantity("81.6", None)


def test_missing_value_parses_to_none_not_zero():
    assert parse_quantity("NA", "kg") is None
    assert parse_quantity(-999, "kg") is None


# --------------------------------------------------------------------------
# BSA
# --------------------------------------------------------------------------

def test_mosteller_bsa():
    # sqrt(180 * 80 / 3600) = sqrt(4) = 2
    assert bsa_mosteller(Decimal("80"), Decimal("180")) == Decimal("2")


def test_bsa_refuses_nonsense_inputs():
    from src.quantities import QuantityError
    with pytest.raises(QuantityError):
        bsa_mosteller(Decimal("0"), Decimal("180"))
