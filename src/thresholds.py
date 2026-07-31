"""Every number that turns evidence into a verdict, in one place.

These are the weakest part of the system and the part a clinical team would
replace first, so they are collected here rather than scattered through the
detectors, each with the reasoning behind the value and what happens if it moves.

None of them is derived from the protocol or from a guideline. They are the
author's choices. A finding that depends on one records which one, so a reader
can see exactly what drove the answer and disagree with it precisely.

Override for a study by passing a `Thresholds` instance to the detectors, or by
placing a `thresholds.json` beside the data with any subset of these keys.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True)
class Threshold:
    name: str
    value: Decimal
    unit: str
    basis: str
    if_it_moves: str

    def describe(self) -> str:
        return f"{self.name} = {self.value}{self.unit}"


@dataclass(frozen=True)
class Thresholds:
    """Illustrative. Not validated by anyone qualified to validate them."""

    dose_tolerance: Decimal = Decimal("0.10")
    dose_important: Decimal = Decimal("0.20")
    systemic_min_subjects: int = 3
    systemic_min_share: Decimal = Decimal("0.5")
    late_entry_days: int = 14
    weight_min_kg: Decimal = Decimal("30")
    weight_max_kg: Decimal = Decimal("250")
    height_min_cm: Decimal = Decimal("120")
    height_max_cm: Decimal = Decimal("230")

    @classmethod
    def load(cls, path: Path | str | None) -> "Thresholds":
        if path is None or not Path(path).exists():
            return cls()
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        known = {f.name: f.type for f in fields(cls)}
        values = {}
        for key, value in raw.items():
            if key not in known:
                continue
            values[key] = int(value) if known[key] is int else Decimal(str(value))
        return cls(**values)

    def to_dict(self) -> dict:
        return {f.name: str(getattr(self, f.name)) for f in fields(self)}


DEFAULTS = Thresholds()

# The provenance of each value, quoted into findings that depend on it.
RATIONALE: dict[str, Threshold] = {
    "dose_tolerance": Threshold(
        "dose tolerance", DEFAULTS.dose_tolerance * 100, "%",
        basis="Author's choice. Real studies define this in the protocol or the "
              "monitoring plan, often per-agent and sometimes asymmetric "
              "(over-dosing tolerated less than under-dosing).",
        if_it_moves="Tighter and rounding or infusion-volume noise becomes a "
                    "deviation; looser and a genuine dosing error is missed.",
    ),
    "dose_important": Threshold(
        "important-deviation dose threshold", DEFAULTS.dose_important * 100, "%",
        basis="Author's choice. E6(R3) defines an important deviation "
              "qualitatively -- one that might significantly affect data "
              "reliability or participant safety -- and gives no percentage.",
        if_it_moves="This is the value that decides what reaches the IRB/EC. "
                    "Moving it changes the sponsor's reported important-deviation "
                    "rate without anything changing at the sites.",
    ),
    "systemic_min_subjects": Threshold(
        "systemic pattern minimum subjects", Decimal(DEFAULTS.systemic_min_subjects),
        " subjects",
        basis="Author's choice. Two subjects is a coincidence; the line between "
              "coincidence and pattern is a judgement, not a fact.",
        if_it_moves="Lower and ordinary variation is escalated as a protocol "
                    "problem; higher and a genuine site-wide issue is filed as N "
                    "separate deviations, which is the wrong remediation.",
    ),
    "systemic_min_share": Threshold(
        "systemic pattern minimum share", DEFAULTS.systemic_min_share * 100, "%",
        basis="Author's choice. Guards against a large site tripping the count "
              "threshold on its size alone.",
        if_it_moves="Same tradeoff as the count, expressed relative to the "
                    "site's cohort.",
    ),
    "late_entry_days": Threshold(
        "late data entry", Decimal(DEFAULTS.late_entry_days), " days",
        basis="Author's choice. Sponsors typically set this in the monitoring "
              "plan, and it varies with study phase and data criticality.",
        if_it_moves="Only affects a data quality observation, never a protocol "
                    "deviation.",
    ),
    "weight_bounds": Threshold(
        "plausible adult weight", Decimal("0"), " (30-250 kg)",
        basis="Author's choice, chosen to bracket adult participants in an "
              "oncology population with margin. A paediatric study would need "
              "different bounds and this value would be actively wrong.",
        if_it_moves="Too narrow and real participants are refused a dose "
                    "assessment; too wide and a decimal slip is dosed on.",
    ),
}


def rationale_for(key: str) -> str:
    entry = RATIONALE.get(key)
    if entry is None:
        return ""
    return (f"{entry.describe()} is an illustrative threshold, not a "
            f"protocol-defined one. {entry.basis}")
