"""Unit-aware Decimal quantities.

The arithmetic layer. No agent, no I/O, no domain knowledge beyond units.

Three rules carried over from the money module, and one added:

1. `Decimal` everywhere. A dosing calculation is not a place for float.
2. Convert at full precision; quantize exactly once, at the boundary, when a
   figure is about to be shown or compared. Intermediate rounding is how a
   correct dose turns into an apparent deviation.
3. A unit is part of the value, never an assumption. `81.6` is not a weight.
4. The many spellings of "missing" collapse to one representation *before* any
   arithmetic. A `-999` reaching a mg/kg calculation is the realistic version
   of a catastrophic bug.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Final


class QuantityError(ValueError):
    """Base for arithmetic-layer failures.

    These are programmer or data errors surfaced to the caller. The graph-facing
    layer (`dosing.py`) converts them into structured results; nothing in this
    module is allowed to reach the graph as an exception.
    """


class UnknownUnit(QuantityError):
    pass


class IncompatibleUnits(QuantityError):
    pass


class Unparseable(QuantityError):
    pass


# --------------------------------------------------------------------------
# Missing-value sentinels
#
# Different sites and different exports mark "missing" differently, and they
# arrive mixed within a single field.
# --------------------------------------------------------------------------

MISSING_TOKENS: Final[frozenset[str]] = frozenset({
    "", ".", "na", "n/a", "nd", "not done", "notdone",
    "unk", "unknown", "null", "none", "missing", "-999", "-999.0",
})

MISSING_NUMBERS: Final[frozenset[Decimal]] = frozenset({Decimal("-999")})


def is_missing(raw: object) -> bool:
    """True for every spelling of "missing" this dataset uses."""
    if raw is None:
        return True
    if isinstance(raw, bool):
        return False
    if isinstance(raw, (int, float, Decimal)):
        try:
            return Decimal(str(raw)) in MISSING_NUMBERS
        except InvalidOperation:
            return True
    if isinstance(raw, str):
        return raw.strip().lower() in MISSING_TOKENS
    return False


# --------------------------------------------------------------------------
# Units
# --------------------------------------------------------------------------

# Every mass unit expressed exactly in milligrams. 1 lb = 0.45359237 kg by
# definition, so the conversion is exact in Decimal and never needs a float.
MASS_IN_MG: Final[dict[str, Decimal]] = {
    "mcg": Decimal("0.001"),
    "mg": Decimal("1"),
    "g": Decimal("1000"),
    "kg": Decimal("1000000"),
    "lb": Decimal("453592.37"),
}

# Spellings seen in the wild, mapped to the canonical form.
UNIT_ALIASES: Final[dict[str, str]] = {
    "ug": "mcg", "µg": "mcg", "μg": "mcg", "microgram": "mcg", "micrograms": "mcg",
    "milligram": "mg", "milligrams": "mg", "mgs": "mg",
    "gram": "g", "grams": "g", "gm": "g",
    "kilogram": "kg", "kilograms": "kg", "kgs": "kg",
    "lbs": "lb", "pound": "lb", "pounds": "lb",
    "mg/m^2": "mg/m2", "mg/m²": "mg/m2", "mg per m2": "mg/m2",
    "mg per kg": "mg/kg", "mg/kilogram": "mg/kg",
    "m^2": "m2", "m²": "m2",
}

# Ratio units: a mass per something. Resolving one to a mass needs a
# subject-specific denominator, which is why they live behind `dosing.py`.
RATIO_UNITS: Final[dict[str, tuple[str, str]]] = {
    "mg/kg": ("mg", "kg"),
    "mg/m2": ("mg", "m2"),
}

_NUMBER_AND_UNIT = re.compile(
    r"^\s*(?P<number>[+-]?\d+(?:[.,]\d+)?)\s*(?P<unit>[a-zA-Zµμ/^²]+\d?)?\s*$"
)


def canonical_unit(raw: object) -> str:
    """Normalise a unit spelling. Raises UnknownUnit for anything unrecognised.

    Deliberately does not default to anything. A missing unit is a question for
    the site, not a value to assume -- that assumption is exactly what turns
    SITE-03's pounds into a fabricated 55% underdose.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise UnknownUnit("no unit given")
    text = str(raw).strip().lower()
    text = UNIT_ALIASES.get(text, text.replace(" ", ""))
    text = UNIT_ALIASES.get(text, text)
    if text in MASS_IN_MG or text in RATIO_UNITS or text == "m2":
        return text
    raise UnknownUnit(f"unrecognised unit: {raw!r}")


def dimension_of(unit: str) -> str:
    if unit in MASS_IN_MG:
        return "mass"
    if unit in RATIO_UNITS:
        return "ratio"
    if unit == "m2":
        return "area"
    raise UnknownUnit(f"unrecognised unit: {unit!r}")


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def parse_decimal(raw: object) -> Decimal | None:
    """Parse a number from JSON that may be a string, int, float or Decimal.

    Returns None when the value means "missing". Raises Unparseable when the
    value is present but not a number -- those are different problems and must
    not be collapsed.
    """
    if is_missing(raw):
        return None
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, bool):
        raise Unparseable(f"boolean is not a quantity: {raw!r}")
    if isinstance(raw, (int, float)):
        return Decimal(str(raw))
    if isinstance(raw, str):
        text = raw.strip().replace(",", ".")
        try:
            return Decimal(text)
        except InvalidOperation as exc:
            raise Unparseable(f"not a number: {raw!r}") from exc
    raise Unparseable(f"not a number: {raw!r}")


@dataclass(frozen=True)
class Quantity:
    """A number that knows what it is."""

    value: Decimal
    unit: str

    def __post_init__(self):
        object.__setattr__(self, "unit", canonical_unit(self.unit))
        if not isinstance(self.value, Decimal):
            object.__setattr__(self, "value", Decimal(str(self.value)))

    @property
    def dimension(self) -> str:
        return dimension_of(self.unit)

    def convert_to(self, unit: str) -> "Quantity":
        """Convert within a dimension, at full precision. Never rounds."""
        target = canonical_unit(unit)
        if dimension_of(self.unit) != dimension_of(target):
            raise IncompatibleUnits(
                f"cannot convert {self.unit} to {target}: different dimensions"
            )
        if self.unit == target:
            return self
        if dimension_of(target) != "mass":
            raise IncompatibleUnits(
                f"cannot convert {self.unit} to {target} without a subject-specific "
                f"denominator; use dosing.normalise_dose"
            )
        in_mg = self.value * MASS_IN_MG[self.unit]
        return Quantity(in_mg / MASS_IN_MG[target], target)

    def quantize(self, places: int = 0) -> "Quantity":
        """Round once, at the boundary. Half-up, as clinical rounding is."""
        exponent = Decimal(1).scaleb(-places)
        return Quantity(self.value.quantize(exponent, rounding=ROUND_HALF_UP), self.unit)

    def __str__(self) -> str:
        return f"{self.value} {self.unit}"


def parse_quantity(raw_value: object, raw_unit: object = None) -> Quantity | None:
    """Build a Quantity from the several encodings this dataset uses.

    `408`, `"408.0"`, `"408 mg"` and `Decimal("408")` with a separate unit field
    all have to reach the same value. Returns None when the value is missing.

    When the value carries its own unit AND a unit field is supplied, the two
    must agree -- the string's unit is not assumed to be authoritative, and a
    disagreement is an error rather than a silent preference.
    """
    if is_missing(raw_value):
        return None

    embedded_unit = None
    if isinstance(raw_value, str):
        match = _NUMBER_AND_UNIT.match(raw_value)
        if match is None:
            raise Unparseable(f"not a quantity: {raw_value!r}")
        number = Decimal(match.group("number").replace(",", "."))
        if match.group("unit"):
            embedded_unit = canonical_unit(match.group("unit"))
    else:
        parsed = parse_decimal(raw_value)
        if parsed is None:
            return None
        number = parsed

    if embedded_unit and not is_missing(raw_unit):
        declared = canonical_unit(raw_unit)
        if declared != embedded_unit:
            raise IncompatibleUnits(
                f"value says {embedded_unit}, unit field says {declared}"
            )
        return Quantity(number, declared)
    if embedded_unit:
        return Quantity(number, embedded_unit)
    if is_missing(raw_unit):
        raise UnknownUnit("value has no unit and no unit field was supplied")
    return Quantity(number, canonical_unit(raw_unit))


# --------------------------------------------------------------------------
# Body surface area
# --------------------------------------------------------------------------

def bsa_mosteller(weight_kg: Decimal, height_cm: Decimal) -> Decimal:
    """Mosteller: BSA(m2) = sqrt(height_cm * weight_kg / 3600).

    Full precision; the caller quantizes at the boundary.
    """
    if weight_kg <= 0 or height_cm <= 0:
        raise QuantityError(
            f"BSA needs positive weight and height, got {weight_kg} kg / {height_cm} cm"
        )
    return (height_cm * weight_kg / Decimal("3600")).sqrt()
