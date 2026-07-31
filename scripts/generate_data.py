"""Deterministic synthetic data generator for the protocol deviation agent.

Two layers, per `pharmagent-data-spec.md`:

- Layer A: pervasive, boring, site-correlated data quality deficiencies. Their
  purpose is to make a fraction of records *unassessable* rather than to hide a
  clever answer.
- Layer B: isolated domain traps, each exercising one piece of detector logic.

Layer A sits on top of Layer B so the traps are harder to see. That is what real
clinical data looks like.

The expected verdicts live in `docs/DATA_TRAPS.md`, rendered from the ISSUES
registry below so the ground truth cannot drift from the data. They are
deliberately NOT written into `data/*.json` — the detectors have to derive them.

Run:  python scripts/generate_data.py
"""

from __future__ import annotations

import json
import random
import re
from collections import Counter, defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DOCS = ROOT / "docs"

SEED = 20260729
LB_PER_KG = Decimal("0.45359237")

# --------------------------------------------------------------------------
# Protocol
# --------------------------------------------------------------------------

# Visit schedule. Windows are deliberately non-uniform: a single global
# tolerance gives wrong answers at both ends (Day 1 is exact, EOT is +/-14).
BASE_SCHEDULE = [
    {
        "visit_id": "SCR",
        "label": "Screening",
        "target_day": None,
        "window_before": None,
        "window_after": None,
        "screening_window": {"earliest_day": -28, "latest_day": -1},
        "required_assessments": ["labs", "eligibility"],
    },
    {"visit_id": "V1", "label": "Day 1", "target_day": 1, "window_before": 0, "window_after": 0,
     "required_assessments": ["labs", "vitals", "dose"]},
    {"visit_id": "V2", "label": "Week 4", "target_day": 29, "window_before": 3, "window_after": 3,
     "required_assessments": ["labs", "vitals", "dose"]},
    {"visit_id": "V3", "label": "Week 8", "target_day": 57, "window_before": 3, "window_after": 3,
     "required_assessments": ["labs", "vitals", "dose"]},
    {"visit_id": "V4", "label": "Week 12", "target_day": 85, "window_before": 3, "window_after": 3,
     "required_assessments": ["labs", "vitals", "dose"]},
    {"visit_id": "V5", "label": "Week 24", "target_day": 169, "window_before": 7, "window_after": 7,
     "required_assessments": ["labs", "vitals", "dose"]},
    {"visit_id": "V6", "label": "End of Treatment", "target_day": 225, "window_before": 14, "window_after": 14,
     "required_assessments": ["labs", "vitals"]},
]

DOSING_BLOCK = {
    "rule": "mg_per_kg",
    "value": "5",
    "unit": "mg/kg",
    "route": "IV",
    "frequency": "every 4 weeks",
    # Trap B1: the programme total is stated; the per-administration expectation
    # is NOT a field. 5 mg/kg x weight-as-of-visit has to be derived, and so
    # does the 28-day cadence (32 weeks / 8 doses).
    "total_doses": 8,
    "treatment_period_weeks": 32,
    # Grade 3 neutropenia (ANC 0.5-1.0) does NOT meet these criteria. That is
    # what makes S-005's withheld dose a deviation rather than per-protocol.
    "hold_criteria": [
        "ANC < 0.5 x10^9/L",
        "Platelets < 50 x10^9/L",
        "Grade 4 non-haematological toxicity",
    ],
}

ELIGIBILITY = {
    "inclusion": [
        "Age >= 18 years at screening",
        "Histologically confirmed advanced solid tumour",
        "ECOG performance status 0-1",
        "Adequate haematological function (ANC >= 1.5 x10^9/L)",
    ],
    "exclusion": [
        "Active CNS metastases",
        "Prior treatment with a study-class agent",
        "Uncontrolled intercurrent illness",
    ],
}


def build_protocol() -> list[dict]:
    v1_schedule = [dict(visit) for visit in BASE_SCHEDULE]

    v2_schedule = [dict(visit) for visit in BASE_SCHEDULE]
    for visit in v2_schedule:
        if visit["visit_id"] == "V2":
            # Amendment 1: widen the Week 4 window.
            visit["window_before"] = 5
            visit["window_after"] = 5
        if visit["visit_id"] == "V4":
            # Amendment 2: add a required ECG at Week 12.
            visit["required_assessments"] = ["labs", "vitals", "dose", "ecg"]

    common = {
        "protocol_id": "PROTO-001",
        "title": "A Phase 2, open-label study of weight-based dosing in advanced solid tumours",
        "phase": 2,
        "design": "open-label",
        "dosing": DOSING_BLOCK,
        "eligibility": ELIGIBILITY,
    }
    return [
        {**common, "version": "1.0", "effective_date": "2025-01-15", "amends": None,
         "visit_schedule": v1_schedule},
        {**common, "version": "2.0", "effective_date": "2025-07-01", "amends": "1.0",
         "amendment_summary": "Week 4 visit window widened from +/-3 to +/-5 days; "
                              "12-lead ECG added as a required assessment at Week 12.",
         "visit_schedule": v2_schedule},
    ]


# --------------------------------------------------------------------------
# Subject roster
#
# Every trap placement is explicit. Only the Layer A scatter uses the RNG, so
# the traps cannot move between runs.
# --------------------------------------------------------------------------

ROSTER = [
    # ---- SITE-01: tidy data, assorted Layer B traps ----
    {"subject_id": "S-001", "site_id": "SITE-01", "version": "1.0",
     "consent": "2025-02-10", "anchor": "2025-02-17", "weight_kg": "72.4", "height_cm": "170",
     "traps": ["missed_visit_v3"]},
    {"subject_id": "S-002", "site_id": "SITE-01", "version": "1.0",
     "consent": "2025-02-20", "anchor": "2025-03-03", "weight_kg": "88.0", "height_cm": "182",
     "traps": ["v3_out_of_window_already_logged"]},
    {"subject_id": "S-003", "site_id": "SITE-01", "version": "2.0",
     "consent": "2025-07-14", "anchor": "2025-07-21", "weight_kg": "64.5", "height_cm": "163",
     "traps": ["missing_ecg_v4"]},
    # Consented under v1.0 eleven days before v2.0 took effect. Their Week 4
    # visit lands in August, when v2.0 is live -- but they keep the +/-3 window.
    {"subject_id": "S-004", "site_id": "SITE-01", "version": "1.0",
     "consent": "2025-06-20", "anchor": "2025-07-07", "weight_kg": "79.2", "height_cm": "176",
     "traps": ["v2_late_4d"]},
    # Same site, same 4-day lateness, consented under v2.0: +/-5, so compliant.
    {"subject_id": "S-009", "site_id": "SITE-01", "version": "2.0",
     "consent": "2025-07-10", "anchor": "2025-07-28", "weight_kg": "70.0", "height_cm": "168",
     "traps": ["v2_late_4d"]},
    {"subject_id": "S-015", "site_id": "SITE-01", "version": "1.0",
     "consent": "2025-02-03", "anchor": "2025-02-24", "weight_kg": "83.6", "height_cm": "179",
     "traps": []},

    # ---- SITE-02: the systemic Week 4 pattern, plus free-text and DD/MM drift ----
    {"subject_id": "S-005", "site_id": "SITE-02", "version": "1.0",
     "consent": "2025-03-05", "anchor": "2025-03-17", "weight_kg": "75.0", "height_cm": "172",
     "traps": ["v2_late_5d", "hazard_dose_withheld_v3"]},
    {"subject_id": "S-006", "site_id": "SITE-02", "version": "1.0",
     "consent": "2025-03-12", "anchor": "2025-03-24", "weight_kg": "68.8", "height_cm": "165",
     "traps": ["v2_late_4d", "consent_sequence"]},
    {"subject_id": "S-008", "site_id": "SITE-02", "version": "1.0",
     "consent": "2025-04-02", "anchor": "2025-04-14", "weight_kg": "91.5", "height_cm": "188",
     "traps": ["v2_late_5d"]},
    {"subject_id": "S-010", "site_id": "SITE-02", "version": "1.0",
     "consent": "2025-04-16", "anchor": "2025-04-28", "weight_kg": "84.0", "height_cm": "181",
     "traps": ["v2_late_4d", "dose_deviation_v4"]},
    {"subject_id": "S-014", "site_id": "SITE-02", "version": "1.0",
     "consent": "2025-05-05", "anchor": "2025-05-19", "weight_kg": "77.3", "height_cm": "174",
     "traps": []},

    # ---- SITE-03: the data quality site (A2, A3, A4, A9) ----
    # Weights in lb. 180 lb = 81.6 kg -> 408 mg. Read as kg it looks like a 55% underdose.
    {"subject_id": "S-007", "site_id": "SITE-03", "version": "1.0",
     "consent": "2025-03-20", "anchor": "2025-04-07", "weight_lb": "180", "height_cm": "177",
     "traps": ["weight_in_lb", "id_format_drift"]},
    {"subject_id": "S-011", "site_id": "SITE-03", "version": "1.0",
     "consent": "2025-05-08", "anchor": None, "weight_kg": "66.2", "height_cm": "160",
     "traps": ["screen_failure"]},
    {"subject_id": "S-012", "site_id": "SITE-03", "version": "2.0",
     "consent": "2025-07-21", "anchor": "2025-08-04", "weight_kg": "80.1", "height_cm": "178",
     "traps": []},
    {"subject_id": "S-013", "site_id": "SITE-03", "version": "1.0",
     "consent": "2025-05-19", "anchor": "2025-06-02", "weight_kg": "73.9", "height_cm": "171",
     "traps": ["layer_a_carrier", "v4_late_42d"]},
]

CLEAN_SUBJECTS = ["S-009", "S-012", "S-014", "S-015"]

# Subjects whose weight records carry a targeted trap. Their measurement dates
# stay precise so the trap remains reachable through the as-of lookup.
WEIGHT_TRAP_SUBJECTS = {"S-007", "S-011", "S-013"}

# Layer A missing-value sentinels. All of these mean "missing"; normalisation
# has to collapse them to one representation.
SENTINELS = ["", None, "NA", "N/A", ".", "UNK", -999, "Not Done", "ND"]

STATUS_DRIFT = ["Randomized", "randomised", "RAND ", "Rand", "COMPLETED", "completed"]

FREE_TEXT = [
    "Patient arrived late due to transport, visit completed same day",
    "site delay - staff shortage",
    "beteg nem jelent meg",           # partial Hungarian, SITE-02
    "see source",
    "Windwo missed by a few days, PI informed",   # typo left in deliberately
    "Rescheduled at participant request",
    "n/a",
]

# --------------------------------------------------------------------------
# Planted-issue registry -> docs/DATA_TRAPS.md
# --------------------------------------------------------------------------

ISSUES: list[dict] = []


def plant(*, layer, code, file, records, subject, verdict, routing, note, expect=None):
    """Record a planted issue so DATA_TRAPS.md is rendered from the same source
    of truth the data was built from.

    `expect` carries the machine-checkable part: which detector should say what
    about which (subject, visit). Without it the ground truth is prose, and a
    prose ground truth can only be diffed by a human reading carefully -- which
    is exactly the check that keeps being skipped.
    """
    ISSUES.append({
        "layer": layer, "code": code, "file": file,
        "records": records if isinstance(records, list) else [records],
        "subject": subject, "verdict": verdict, "routing": routing, "note": note,
        "expect": expect or [],
    })


def expectation(subject, visit, verdict, detector=None, *,
                actions=None, forbidden_actions=None, must_cite=None, suppressed=None):
    """One machine-checkable claim about what the detectors should produce."""
    return {
        "subject_id": subject, "visit_id": visit, "verdict": verdict,
        "detector": detector, "actions": actions or [],
        "forbidden_actions": forbidden_actions or [],
        "must_cite": must_cite or [], "suppressed": suppressed,
    }


# --------------------------------------------------------------------------
# Schedule arithmetic
# --------------------------------------------------------------------------

def schedule_for(version: str) -> list[dict]:
    return [v for v in build_protocol() if v["version"] == version][0]["visit_schedule"]


def target_date(anchor: date, target_day: int) -> date:
    """Day 1 is the anchor (first dose). Day N is anchor + (N-1) days."""
    return anchor + timedelta(days=target_day - 1)


def expected_dose_mg(weight_kg: Decimal) -> Decimal:
    """5 mg/kg, quantized once at the end."""
    return (Decimal("5") * weight_kg).quantize(Decimal("1"))


def lb_to_kg(pounds: Decimal) -> Decimal:
    return pounds * LB_PER_KG


# --------------------------------------------------------------------------
# Record construction
# --------------------------------------------------------------------------

class Builder:
    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.subjects: list[dict] = []
        self.visits: list[dict] = []
        self.dosing: list[dict] = []
        self.vitals: list[dict] = []
        self.deviation_log: list[dict] = []
        self._counters: Counter = Counter()

    def next_id(self, prefix: str) -> str:
        self._counters[prefix] += 1
        return f"{prefix}-{self._counters[prefix]:04d}"

    # -- entry lag: SITE-03 enters data 30-90 days late (A9) ---------------
    def entered_date(self, site_id: str, when: date) -> str:
        if site_id == "SITE-03":
            lag = self.rng.randint(30, 90)
        else:
            lag = self.rng.randint(0, 6)
        return (when + timedelta(days=lag)).isoformat()

    # -- subjects ----------------------------------------------------------
    def build_subjects(self):
        for row in ROSTER:
            anchor = row["anchor"]
            screen_failure = "screen_failure" in row["traps"]
            consent = date.fromisoformat(row["consent"])

            if screen_failure:
                status = "Screen Failure"
            else:
                status = STATUS_DRIFT[self.rng.randrange(len(STATUS_DRIFT))] \
                    if row["site_id"] == "SITE-03" else "Randomized"

            record = {
                "subject_id": row["subject_id"],
                "site_id": row["site_id"],
                "consent_date": row["consent"],
                "protocol_version_consented": row["version"],
                "anchor_date": anchor,
                "enrollment_status": status,
                "screen_failure_reason": (
                    "Inclusion criterion 4 not met: ANC 1.1 x10^9/L at screening"
                    if screen_failure else None
                ),
                "entered_date": self.entered_date(row["site_id"], consent),
            }
            self.subjects.append(record)

        plant(layer="B", code="B3 screen failure with data", file="subjects.json / visits.json",
              records=["S-011"], subject="S-011", verdict="compliant",
              routing="none - no compliance findings apply",
              note="Consented, screened, laboratory data present, failed inclusion criterion 4, "
                   "never dosed and never randomised. Protocol compliance does not apply to a "
                   "subject who was never enrolled. Any deviation finding against S-011 is a "
                   "false positive.",
              expect=[expectation("S-011", None, "compliant")])

        plant(layer="A", code="A6 case/whitespace drift in categoricals", file="subjects.json",
              records=["SITE-03 subjects"], subject="SITE-03", verdict="compliant",
              routing="none - normalisation only",
              note="enrollment_status appears as Randomized / randomised / 'RAND ' / Rand / "
                   "COMPLETED / completed. Must normalise; must not be read as different states.")

    # -- visits, dosing, vitals -------------------------------------------
    def build_subject_records(self, row: dict):
        subject_id = row["subject_id"]
        site_id = row["site_id"]
        traps = row["traps"]
        consent = date.fromisoformat(row["consent"])
        schedule = schedule_for(row["version"])

        # Screening always happens; it is how S-011 has data without enrolment.
        if row["anchor"] is None:
            screening_date = consent + timedelta(days=6)
            self.visits.append(self._visit(subject_id, site_id, "SCR", "Screening",
                                           screening_date, ["labs", "eligibility"], "Completed"))
            self.vitals.append(self._vitals(subject_id, site_id, "SCR", screening_date, row))
            return

        anchor = date.fromisoformat(row["anchor"])
        # Screening sits in the 28 days before Day 1, but it must also fall
        # AFTER consent. A fixed anchor-14 offset silently puts screening before
        # consent for every subject enrolled quickly, which manufactures
        # consent-sequence deviations and drowns the one deliberate trap.
        gap = (anchor - consent).days
        screening_date = anchor - timedelta(days=min(14, max(gap - 1, 1)))

        # Trap B10: a screening procedure dated one day BEFORE consent.
        if "consent_sequence" in traps:
            screening_date = consent - timedelta(days=1)
            plant(layer="B", code="B10 consent sequence", file="visits.json",
                  records=[f"{subject_id} SCR"], subject=subject_id, verdict="deviation",
                  routing="deviation log (proposed: important) + escalate_to_medical_monitor",
                  note=f"Screening visit dated {screening_date.isoformat()}, one day before the "
                       f"consent date {consent.isoformat()}. A study procedure performed before "
                       f"informed consent affects participant rights -- proposed important.",
              expect=[expectation(subject_id, "SCR", "deviation", "consent_sequence",
                                      actions=["log_deviation", "escalate_to_medical_monitor"])])

        self.visits.append(self._visit(subject_id, site_id, "SCR", "Screening",
                                       screening_date, ["labs", "eligibility"], "Completed"))
        self.vitals.append(self._vitals(subject_id, site_id, "SCR", screening_date, row))

        for visit in schedule:
            if visit["visit_id"] == "SCR":
                continue
            vid = visit["visit_id"]

            # Trap B12: Week 8 absent entirely, window long closed.
            if vid == "V3" and "missed_visit_v3" in traps:
                plant(layer="B", code="B12 missed visit", file="visits.json",
                      records=[f"{subject_id} V3 (absent)"], subject=subject_id,
                      verdict="deviation", routing="raise_site_query, then deviation log",
                      note=f"No Week 8 record exists for {subject_id} and the window "
                           f"({target_date(anchor, 57).isoformat()} +/-3) closed long ago. "
                           f"Absence of a record is only a finding once the window is shut.",
              expect=[expectation(subject_id, "V3", "deviation", "missed_visit")])
                continue

            offset = self._lateness(vid, traps)
            actual = target_date(anchor, visit["target_day"]) + timedelta(days=offset)
            assessments = list(visit["required_assessments"])

            # Trap B11: Week 12 happened, but the v2.0-required ECG is absent.
            if vid == "V4" and "missing_ecg_v4" in traps:
                assessments = [a for a in assessments if a != "ecg"]
                plant(layer="B", code="B11 missing required assessment", file="visits.json",
                      records=[f"{subject_id} V4"], subject=subject_id, verdict="deviation",
                      routing="raise_site_query (proposed: not important pending PI review)",
                      note=f"{subject_id} is governed by v2.0, which requires a 12-lead ECG at "
                           f"Week 12. The visit occurred on {actual.isoformat()} and recorded "
                           f"labs/vitals/dose but no ECG. A v1.0 subject would be compliant here.",
              expect=[expectation(subject_id, "V4", "deviation", "missing_assessment")])

            self.visits.append(self._visit(subject_id, site_id, vid, visit["label"],
                                           actual, assessments, "Completed"))
            self.vitals.append(self._vitals(subject_id, site_id, vid, actual, row))

            if "dose" in visit["required_assessments"]:
                self._build_dose(row, vid, actual, traps)

        self._plant_lateness_notes(row, anchor, traps)

    def _lateness(self, visit_id: str, traps: list[str]) -> int:
        if visit_id == "V2":
            if "v2_late_4d" in traps:
                return 4
            if "v2_late_5d" in traps:
                return 5
        if visit_id == "V3" and "v3_out_of_window_already_logged" in traps:
            return 6
        # Genuinely six weeks late. The record is later degraded to a
        # month-precision date, but every day in that month is still outside
        # the window, so the verdict survives the imprecision.
        if visit_id == "V4" and "v4_late_42d" in traps:
            return 42
        return 0

    def _plant_lateness_notes(self, row, anchor, traps):
        subject_id = row["subject_id"]
        version = row["version"]
        window = 5 if version == "2.0" else 3

        if "v2_late_4d" in traps or "v2_late_5d" in traps:
            days = 4 if "v2_late_4d" in traps else 5
            actual = target_date(anchor, 29) + timedelta(days=days)
            compliant = days <= window
            if subject_id in ("S-004", "S-009"):
                plant(layer="B", code="B2 protocol version lineage",
                      file="subjects.json / visits.json", records=[f"{subject_id} V2"],
                      subject=subject_id,
                      verdict="compliant" if compliant else "deviation",
                      routing="none" if compliant else "deviation log (proposed: not important)",
                      note=f"{subject_id} consented {row['consent']} under v{version} and is "
                           f"governed by v{version} (+/-{window}), not by the version in force on "
                           f"the calendar date of the visit. Week 4 target "
                           f"{target_date(anchor, 29).isoformat()}, actual {actual.isoformat()}, "
                           f"{days} days late. S-004 and S-009 are {days} days late by the same "
                           f"calendar arithmetic and reach OPPOSITE verdicts. Measuring S-004 "
                           f"against v2.0 would suppress a real deviation; measuring S-009 "
                           f"against v1.0 would fabricate one.",
              expect=[expectation(subject_id, "V2", "compliant" if compliant else "deviation",
                            None if compliant else "out_of_window_visit")])
            else:
                plant(layer="B", code="B9 systemic pattern (SITE-02 Week 4)", file="visits.json",
                      records=[f"{subject_id} V2"], subject=subject_id, verdict="deviation",
                      routing="SUPPRESSED into propose_protocol_amendment + open_capa",
                      note=f"Week 4 {days} days late against a +/-{window} window. One of four "
                           f"such deviations at SITE-02. Individually a deviation; correctly "
                           f"reported it is subsumed by the site-level pattern.",
              expect=[expectation(subject_id, "V2", "deviation", "out_of_window_visit",
                            suppressed=True)])

        if "v3_out_of_window_already_logged" in traps:
            actual = target_date(anchor, 57) + timedelta(days=6)
            plant(layer="B", code="B4 already logged", file="visits.json / deviation_log.json",
                  records=[f"{subject_id} V3", "DEV-0001"], subject=subject_id,
                  verdict="deviation", routing="none - already recorded, must NOT be re-filed",
                  note=f"Week 8 performed {actual.isoformat()}, 6 days after the target against a "
                       f"+/-3 window. The deviation is genuine, but DEV-0001 already records it. "
                       f"Filing it again double-reports and inflates the site's deviation rate.",
              expect=[expectation(subject_id, "V3", "deviation", "out_of_window_visit",
                            suppressed=True)])

    def _build_dose(self, row, visit_id, when, traps):
        subject_id = row["subject_id"]
        site_id = row["site_id"]
        weight_kg = self._weight_kg(row, visit_id)

        # Trap B8: dose withheld on clinical grounds. ICH E6(R3) 2.5.3.
        if visit_id == "V3" and "hazard_dose_withheld_v3" in traps:
            self.dosing.append({
                "dosing_record_id": self.next_id("DR"),
                "subject_id": subject_id,
                "visit_id": visit_id,
                "dose_date": when.isoformat(),
                "dose_administered": None,
                "dose_unit": "mg",
                "dose_status": "Withheld",
                "reason": "Dose withheld by investigator. Grade 3 neutropenia, ANC 0.8 x10^9/L "
                          "on day of visit. Held to avoid immediate hazard to the participant; "
                          "medical monitor notified same day.",
                "entered_date": self.entered_date(site_id, when),
            })
            plant(layer="B", code="B8 immediate hazard (E6(R3) 2.5.3)", file="dosing.json",
                  records=[f"{subject_id} V3 dose"], subject=subject_id, verdict="deviation",
                  routing="deviation log ONLY - no CAPA, no corrective action against the site",
                  note="The protocol's hold criteria are ANC < 0.5 x10^9/L. This participant's "
                       "ANC was 0.8 (grade 3), which does NOT meet the protocol threshold, so "
                       "withholding the dose departed from the protocol and IS a deviation. It "
                       "was made to eliminate an immediate hazard to the participant, which "
                       "ICH E6(R3) 2.5.3 permits. It must be documented; it must NOT generate a "
                       "corrective action against a site that did exactly the right thing. The "
                       "finding text must cite 2.5.3.",
              expect=[expectation(subject_id, "V3", "deviation", "dose_deviation",
                                    actions=["log_deviation"],
                                    forbidden_actions=["open_capa"],
                                    must_cite=["2.5.3"])])
            return

        dose = expected_dose_mg(weight_kg)

        # A deliberate, safety-relevant underdose.
        if visit_id == "V4" and "dose_deviation_v4" in traps:
            administered = (dose * Decimal("0.71")).quantize(Decimal("1"))
            self.dosing.append({
                "dosing_record_id": self.next_id("DR"),
                "subject_id": subject_id,
                "visit_id": visit_id,
                "dose_date": when.isoformat(),
                "dose_administered": str(administered),
                "dose_unit": "mg",
                "dose_status": "Administered",
                "reason": "Infusion stopped early, remainder not administered",
                "entered_date": self.entered_date(site_id, when),
            })
            pct = ((dose - administered) / dose * Decimal("100")).quantize(Decimal("0.1"))
            plant(layer="B", code="B-dose dose deviation", file="dosing.json",
                  records=[f"{subject_id} V4 dose"], subject=subject_id, verdict="deviation",
                  routing="deviation log (proposed: important) + escalate_to_medical_monitor",
                  note=f"Weight as of {when.isoformat()} is {weight_kg} kg, so 5 mg/kg expects "
                       f"{dose} mg. {administered} mg was administered -- {pct}% below expected. "
                       f"Underdosing affects data reliability and possibly participant benefit.",
              expect=[expectation(subject_id, "V4", "deviation", "dose_deviation",
                                    actions=["escalate_to_medical_monitor"])])
            return

        self.dosing.append({
            "dosing_record_id": self.next_id("DR"),
            "subject_id": subject_id,
            "visit_id": visit_id,
            "dose_date": when.isoformat(),
            "dose_administered": str(dose),
            "dose_unit": "mg",
            "dose_status": "Administered",
            "reason": None,
            "entered_date": self.entered_date(site_id, when),
        })

    def _weight_kg(self, row, visit_id) -> Decimal:
        """Weight drifts across the study, so the as-of lookup matters."""
        if "weight_lb" in row:
            return lb_to_kg(Decimal(row["weight_lb"]))
        base = Decimal(row["weight_kg"])
        drift = {"SCR": "0.0", "V1": "0.0", "V2": "-0.6", "V3": "-1.2",
                 "V4": "-1.9", "V5": "-2.4", "V6": "-2.8"}[visit_id]
        return (base + Decimal(drift)).quantize(Decimal("0.1"))

    def _visit(self, subject_id, site_id, visit_id, label, when, assessments, status):
        return {
            "visit_record_id": self.next_id("VR"),
            "subject_id": subject_id,
            "site_id": site_id,
            "visit_id": visit_id,
            "visit_label": label,
            "visit_date": when.isoformat(),
            "assessments_done": assessments,
            "status": status,
            "entered_date": self.entered_date(site_id, when),
            "comment": None,
        }

    def _vitals(self, subject_id, site_id, visit_id, when, row):
        weight_kg = self._weight_kg(row, visit_id)
        if "weight_lb" in row:
            weight, unit = row["weight_lb"], "lb"
        else:
            weight, unit = str(weight_kg), "kg"
        return {
            "vitals_record_id": self.next_id("VT"),
            "subject_id": subject_id,
            "site_id": site_id,
            "visit_id": visit_id,
            "measured_date": when.isoformat(),
            "weight": weight,
            "weight_unit": unit,
            "height_cm": row["height_cm"],
            "entered_date": self.entered_date(site_id, when),
        }

    # -- deviation log -----------------------------------------------------
    def build_deviation_log(self):
        s002 = [r for r in ROSTER if r["subject_id"] == "S-002"][0]
        anchor = date.fromisoformat(s002["anchor"])
        actual = target_date(anchor, 57) + timedelta(days=6)
        self.deviation_log.append({
            "deviation_id": "DEV-0001",
            "subject_id": "S-002",
            "site_id": "SITE-01",
            "visit_id": "V3",
            "deviation_date": actual.isoformat(),
            "category": "visit_out_of_window",
            "description": "Week 8 visit performed 6 days after the protocol-specified target "
                           "day against a +/-3 day window.",
            "classification": "not important",
            "classified_by": "Dr A. Kovacs (Principal Investigator)",
            "logged_date": (actual + timedelta(days=9)).isoformat(),
            "capa_required": False,
        })


# --------------------------------------------------------------------------
# Layer A injection
#
# Explicit rather than probabilistic, so the not_assessable rate is controlled
# and DATA_TRAPS.md can name exact records.
# --------------------------------------------------------------------------

class LayerA:
    # Every alias SITE-03 uses for S-007.
    S007_ALIASES = {"S-007", "S007", "007", "SITE03-007", "s-007"}

    @staticmethod
    def _canonical_subject(record) -> str:
        raw = str(record.get("subject_id") or "")
        digits = "".join(ch for ch in raw if ch.isdigit())
        # 'SITE03-007' carries the site number too; the subject is the tail.
        return f"S-{int(digits[-3:]):03d}" if digits else ""

    def __init__(self, builder: Builder):
        self.b = builder
        self.rng = builder.rng
        # Two kinds of assessable unit: a visit (can it be placed in its
        # window?) and a dose (can the expected mg be computed?). Counting only
        # visits hides every weight problem, which is most of SITE-03's damage.
        self.unassessable: set[str] = set()
        self.unassessable_doses: set[str] = set()

    def mark_dose_unassessable(self, subject_id, visit_id, reason_owner=None):
        """A weight problem blocks the dose assessment at that visit."""
        aliases = self.S007_ALIASES if subject_id == "S-007" else {subject_id}
        for record in self.b.dosing:
            if record["subject_id"] in aliases and record["visit_id"] == visit_id:
                self.unassessable_doses.add(record["dosing_record_id"])
                return record["dosing_record_id"]
        return None

    def find_visit(self, subject_id, visit_id):
        for record in self.b.visits:
            if record["subject_id"] == subject_id and record["visit_id"] == visit_id:
                return record
        raise KeyError(f"{subject_id}/{visit_id}")

    def find_vitals(self, subject_id, visit_id):
        for record in self.b.vitals:
            if record["subject_id"] == subject_id and record["visit_id"] == visit_id:
                return record
        raise KeyError(f"{subject_id}/{visit_id}")

    def find_dose(self, subject_id, visit_id):
        for record in self.b.dosing:
            if record["subject_id"] == subject_id and record["visit_id"] == visit_id:
                return record
        raise KeyError(f"{subject_id}/{visit_id}")

    # -- A1: partial and missing dates -------------------------------------
    def partial_dates(self):
        targets = [
            ("S-013", "V2", "2025-06"),       # month known, day not
            ("S-013", "V5", "2025-11-UN"),    # sponsor-style unknown day
            ("S-007", "V4", "2025-06"),
            ("S-007", "V5", "2025"),          # year only
            ("S-011", "SCR", "2025-05"),
        ]
        for subject_id, visit_id, partial in targets:
            record = self.find_visit(subject_id, visit_id)
            original = record["visit_date"]
            record["visit_date"] = partial
            self.unassessable.add(record["visit_record_id"])
            plant(layer="A", code="A1 partial / missing date", file="visits.json",
                  records=[record["visit_record_id"]], subject=subject_id,
                  verdict="not_assessable", routing="raise_site_query",
                  note=f"{visit_id} visit_date recorded as '{partial}' (true value "
                       f"{original}). A date without a day cannot be placed inside a +/-3 day "
                       f"window. Imputing a day and then issuing a deviation verdict would be "
                       f"a fabricated finding; per CDISC guidance, reflect what is known.",
              expect=[expectation(subject_id, visit_id, "not_assessable")])

        # A date that is partial but still decidable: the visit was genuinely
        # six weeks late, and every day in the recorded month falls outside the
        # window, so the verdict does not depend on the missing day.
        record = self.find_visit("S-013", "V4")
        original = record["visit_date"]
        record["visit_date"] = original[:7]
        target = target_date(date.fromisoformat("2025-06-02"), 85)
        plant(layer="A", code="A1 partial date, still decidable", file="visits.json",
              records=[record["visit_record_id"]], subject="S-013", verdict="deviation",
              routing="deviation log, flagged precision=month, imputed=false",
              note=f"Week 12 recorded as '{record['visit_date']}' (true value {original}); the "
                   f"target was {target.isoformat()} +/-3, i.e. "
                   f"{(target - timedelta(days=3)).isoformat()} to "
                   f"{(target + timedelta(days=3)).isoformat()}. Every day in the recorded month "
                   f"falls outside that window, so the verdict holds regardless of the missing "
                   f"day and no imputation is needed. A system that treats every partial date as "
                   f"unassessable is as wrong as one that imputes -- this record separates them.",
              expect=[expectation("S-013", "V4", "deviation", "out_of_window_visit")])


    def _scatter_partial_dates(self):
        """Degrade ~8% of all dated fields to a partial ISO form."""
        dated = []
        for record in self.b.visits:
            dated += [(record, "entered_date")]
        for record in self.b.vitals:
            dated += [(record, "entered_date"), (record, "measured_date")]
        for record in self.b.dosing:
            dated += [(record, "entered_date")]
        for record in self.b.subjects:
            dated += [(record, "entered_date")]
        # The four clean subjects stay clean. A dataset needs a control group.
        dated = [item for item in dated
                 if item[0].get("subject_id") not in CLEAN_SUBJECTS]
        # Do not degrade a measurement date that a targeted weight trap depends
        # on. If S-007's Day 1 measured_date loses its day, the as-of lookup
        # falls back to the screening weight and the lb trap stops testing what
        # it exists to test; if S-013's -999 record loses its date, the exact-
        # date rejection turns into a silent fallback. Layer A is meant to
        # obscure Layer B, not to disarm it.
        dated = [item for item in dated
                 if not (item[1] == "measured_date"
                         and self._canonical_subject(item[0]) in WEIGHT_TRAP_SUBJECTS)]

        def weight(item):
            return 3 if item[0].get("site_id") == "SITE-03" else 1

        total_dated_fields = len(dated) + len(self.b.visits) + len(self.b.dosing) \
            + len(self.b.subjects)  # + visit_date, dose_date, consent_date
        budget = round(total_dated_fields * 0.09) - 6  # 6 already spent above
        pool = [item for item in dated for _ in range(weight(item))]
        chosen, seen = [], set()
        self.rng.shuffle(pool)
        for record, field in pool:
            key = (id(record), field)
            if key in seen or not isinstance(record.get(field), str):
                continue
            if len(record[field]) != 10:
                continue
            seen.add(key)
            chosen.append((record, field))
            if len(chosen) >= budget:
                break

        forms = 0
        for index, (record, field) in enumerate(chosen):
            value = record[field]
            style = index % 3
            record[field] = value[:7] if style == 0 else (
                f"{value[:7]}-UN" if style == 1 else value[:4]
            )
            forms += 1
        self._partial_scatter_count = forms
        self._dated_field_total = total_dated_fields
        plant(layer="A", code="A1 partial dates at scale", file="all files",
              records=[f"{forms} fields across visits/vitals/dosing/subjects"],
              subject="(all sites, weighted to SITE-03)", verdict="compliant",
              routing="none - parsing requirement",
              note=f"{forms} of {total_dated_fields} dated fields "
                   f"({forms / total_dated_fields * 100:.1f}%) carry a partial ISO value: "
                   f"'2025-06', '2025-06-UN' or '2025'. These sit in entry and measurement "
                   f"dates where imprecision does not change a verdict, but every one of them "
                   f"must parse without crashing and must retain its precision. The "
                   f"verdict-bearing partial dates are listed separately above.")

    # -- A2: inconsistent missing-value sentinels --------------------------
    def sentinels(self):
        # The dangerous one: -999 entering a mg/kg calculation.
        record = self.find_vitals("S-013", "V3")
        record["weight"] = -999
        dose_id = self.mark_dose_unassessable("S-013", "V3")
        plant(layer="A", code="A2 sentinel in a computed field",
              file="vitals.json / dosing.json",
              records=[record["vitals_record_id"], dose_id], subject="S-013",
              verdict="not_assessable", routing="raise_site_query",
              note="weight = -999. Entering a mg/kg calculation unchecked this yields an "
                   "expected dose of -4995 mg, and the administered dose then looks wildly "
                   "over. Sentinels must map to missing BEFORE any arithmetic, and a missing "
                   "weight makes the dose not_assessable -- not zero, and not a deviation.",
              expect=[expectation("S-013", "V3", "not_assessable", "dose_deviation")])

        record = self.find_vitals("S-007", "V3")
        record["weight"] = "NA"
        dose_id = self.mark_dose_unassessable("S-007", "V3")
        plant(layer="A", code="A2 sentinel in a computed field",
              file="vitals.json / dosing.json",
              records=[record["vitals_record_id"], dose_id], subject="S-007",
              verdict="not_assessable", routing="raise_site_query",
              note="weight = 'NA'. The Week 8 dose cannot be assessed without a weight. Note "
                   "the as-of rule does not rescue this: the Week 4 weight is not the Week 8 "
                   "weight, and substituting it silently would be an undeclared imputation.",
              expect=[expectation("S-007", "V3", "not_assessable", "dose_deviation")])

        # Harmless sentinels in optional fields: ~12%, all sites. Must
        # normalise; must not be read as content.
        markers = ["", None, "NA", "N/A", ".", "UNK", -999, "Not Done", "ND"]
        optional_fields = [(r, "comment") for r in self.b.visits
                           if r.get("subject_id") not in CLEAN_SUBJECTS] \
            + [(r, "reason") for r in self.b.dosing
               if r.get("subject_id") not in CLEAN_SUBJECTS]
        touched, by_marker = 0, Counter()
        for record, field in optional_fields:
            threshold = 0.19 if record.get("site_id") == "SITE-03" else 0.10
            if self.rng.random() < threshold:
                marker = markers[self.rng.randrange(len(markers))]
                record[field] = marker
                by_marker[repr(marker)] += 1
                touched += 1
        self._sentinel_share = touched / len(optional_fields) * 100
        plant(layer="A", code="A2 sentinels in optional fields",
              file="visits.json / dosing.json",
              records=[f"{touched} of {len(optional_fields)} optional fields "
                       f"({self._sentinel_share:.1f}%)"],
              subject="(all sites, weighted to SITE-03)",
              verdict="compliant", routing="none - normalisation only",
              note="'' / null / NA / N/A / . / UNK / -999 / Not Done / ND all mean missing, and "
                   "they arrive mixed within the same field. They must collapse to one "
                   "representation. Observed spread: "
                   + ", ".join(f"{k} x{v}" for k, v in sorted(by_marker.items())) + ".")

    # -- A3: subject ID format drift, SITE-03 only -------------------------
    def id_drift(self):
        forms = {"visits": "S007", "dosing": "SITE03-007", "vitals": "s-007"}
        counts = Counter()
        for record in self.b.visits:
            if record["subject_id"] == "S-007":
                record["subject_id"] = forms["visits"]
                counts["visits"] += 1
        for record in self.b.dosing:
            if record["subject_id"] == "S-007":
                record["subject_id"] = forms["dosing"]
                counts["dosing"] += 1
        for record in self.b.vitals:
            if record["subject_id"] == "S-007":
                record["subject_id"] = forms["vitals"]
                counts["vitals"] += 1
        # One more form, on a single record, so all four appear.
        self.b.visits[[i for i, r in enumerate(self.b.visits)
                       if r["subject_id"] == "S007"][0]]["subject_id"] = "007"

        plant(layer="A", code="A3 subject ID format drift", file="visits/dosing/vitals.json",
              records=[f"S007 x{counts['visits'] - 1}, 007 x1, "
                       f"SITE03-007 x{counts['dosing']}, s-007 x{counts['vitals']}"],
              subject="S-007", verdict="compliant",
              routing="data quality finding -> open_capa (site level), NOT a deviation",
              note="subjects.json says S-007; SITE-03 writes S007, 007, SITE03-007 and s-007 "
                   "across the other files for the same person. The join must normalise, and "
                   "must report how many records matched only after normalisation -- that count "
                   "is itself a finding. Other sites are consistent.")

    # -- A4: units missing or embedded in the value -------------------------
    def unit_drift(self):
        record = self.find_vitals("S-013", "V2")
        record.pop("weight_unit")
        self.mark_dose_unassessable("S-013", "V2")
        plant(layer="A", code="A4 missing unit", file="vitals.json / dosing.json",
              records=[record["vitals_record_id"]], subject="S-013",
              verdict="not_assessable", routing="raise_site_query",
              note=f"weight = {record['weight']} with no weight_unit field at all. Assuming kg "
                   f"is exactly the mistake the lb site exists to punish. A missing unit is "
                   f"not_assessable, not an assumed kg.",
              expect=[expectation("S-013", "V2", "not_assessable", "dose_deviation")])

        # Same quantity, three encodings. All parseable; none may change a verdict.
        variants = []
        record = self.find_dose("S-013", "V1")
        record["dose_administered"] = f"{record['dose_administered']} mg"
        variants.append(f"S-013 V1 = '{record['dose_administered']}' (unit in the string)")
        record = self.find_dose("S-013", "V4")
        record["dose_administered"] = f"{Decimal(record['dose_administered']):.1f}"
        variants.append(f"S-013 V4 = '{record['dose_administered']}' (trailing .0)")
        record = self.find_dose("S-001", "V2")
        record["dose_administered"] = int(Decimal(record["dose_administered"]))
        variants.append(f"S-001 V2 = {record['dose_administered']} (bare integer)")
        plant(layer="A", code="A4 unit embedded in the value", file="dosing.json",
              records=variants, subject="S-013 / S-001",
              verdict="compliant", routing="none - normalisation only",
              note="dose_administered arrives as a unit-bearing string, a float-formatted "
                   "string and a bare JSON integer. All three must normalise to the same "
                   "Decimal and reach the same verdict. Note the string form carries its own "
                   "unit, which must be checked against dose_unit rather than assumed to agree.")

    # -- A5: duplicate records with drift ----------------------------------
    def duplicates(self):
        for subject_id, visit_id in [("S-008", "V3"), ("S-013", "V3"), ("S-002", "V5")]:
            original = self.find_visit(subject_id, visit_id)
            copy = dict(original)
            copy["visit_record_id"] = self.b.next_id("VR")
            if not str(original["visit_date"]).endswith("UN") and len(str(original["visit_date"])) == 10:
                copy["visit_date"] = (
                    date.fromisoformat(original["visit_date"]) + timedelta(days=1)
                ).isoformat()
            # The original's entered_date may itself have been degraded to a
            # partial form by the A1 scatter; only shift it when it is a full date.
            try:
                copy["entered_date"] = (
                    date.fromisoformat(original["entered_date"]) + timedelta(days=14)
                ).isoformat()
            except ValueError:
                copy["entered_date"] = original["entered_date"]
            copy["comment"] = "Corrected entry"
            self.b.visits.append(copy)
            self.unassessable.add(copy["visit_record_id"])
            self.unassessable.add(original["visit_record_id"])
            plant(layer="A", code="A5 duplicate record with drift", file="visits.json",
                  records=[original["visit_record_id"], copy["visit_record_id"]],
                  subject=subject_id, verdict="not_assessable", routing="raise_site_query",
                  note=f"The same {visit_id} visit entered twice, dates one day apart, the second "
                       f"entered two weeks later without the first being voided. Which date is "
                       f"authoritative is a question for the site. Do not silently pick one, and "
                       f"do not count the visit twice.",
              expect=[expectation(subject_id, visit_id, "not_assessable", "out_of_window_visit")])

    # -- A7: free text where a code belongs --------------------------------
    def free_text(self):
        candidates = [r for r in self.b.visits
                      if r["site_id"] in ("SITE-02", "SITE-01")
                      and r.get("subject_id") not in CLEAN_SUBJECTS]
        chosen = [candidates[i] for i in
                  sorted(self.rng.sample(range(len(candidates)), 7))]
        for index, record in enumerate(chosen):
            record["comment"] = FREE_TEXT[index % len(FREE_TEXT)]
        hungarian = [r for r in self.b.visits
                     if r["site_id"] == "SITE-02" and r["visit_id"] == "V3"][0]
        hungarian["comment"] = "beteg nem jelent meg"
        plant(layer="A", code="A7 free text where a code belongs", file="visits.json",
              records=[r["visit_record_id"] for r in chosen] + [hungarian["visit_record_id"]],
              subject="SITE-01 / SITE-02", verdict="compliant",
              routing="quote as evidence only",
              note="comment fields contain prose of varying quality, one entry in Hungarian "
                   "('beteg nem jelent meg' = 'the patient did not attend'), one typo "
                   "('Windwo'), one that is just 'see source'. Never parse meaning out of free "
                   "text to drive a verdict. Quote it as evidence for a human.")

    # -- A8: referential gaps ----------------------------------------------
    def referential_gaps(self):
        orphan = dict(self.b.visits[0])
        orphan["visit_record_id"] = self.b.next_id("VR")
        orphan["subject_id"] = ""
        orphan["site_id"] = "SITE-03"
        orphan["visit_id"] = "V2"
        orphan["visit_label"] = "Week 4"
        orphan["visit_date"] = "2025-09-16"
        orphan["comment"] = "see source"
        self.b.visits.append(orphan)
        self.unassessable.add(orphan["visit_record_id"])
        plant(layer="B", code="B5 orphan record (no subject)", file="visits.json",
              records=[orphan["visit_record_id"]], subject="(none)",
              verdict="not_assessable", routing="escalate - no automated remedy",
              note="A Week 4 visit with an empty subject_id. There is no defensible way to "
                   "guess whose visit this is. Escalate; never attribute it by proximity.",
              expect=[expectation(None, "V2", "not_assessable", "unattributable_record")])

        unknown = dict(orphan)
        unknown["visit_record_id"] = self.b.next_id("VR")
        unknown["subject_id"] = "S-099"
        unknown["visit_date"] = "2025-09-23"
        unknown["comment"] = None
        self.b.visits.append(unknown)
        self.unassessable.add(unknown["visit_record_id"])

        ghost_dose = {
            "dosing_record_id": self.b.next_id("DR"),
            "subject_id": "S-010",
            "visit_id": "V5",
            "dose_date": "2025-10-27",
            "dose_administered": "405",
            "dose_unit": "mg",
            "dose_status": "Administered",
            "reason": None,
            "entered_date": "2025-11-02",
        }
        # Remove the visit this dose claims to belong to.
        self.b.visits = [r for r in self.b.visits
                         if not (r["subject_id"] == "S-010" and r["visit_id"] == "V5")]
        self.b.dosing = [r for r in self.b.dosing
                         if not (r["subject_id"] == "S-010" and r["visit_id"] == "V5")]
        self.b.dosing.append(ghost_dose)
        plant(layer="A", code="A8 referential gap", file="visits.json / dosing.json",
              records=[unknown["visit_record_id"], ghost_dose["dosing_record_id"]],
              subject="S-099 / S-010", verdict="not_assessable", routing="raise_site_query",
              note="A visit for subject S-099, who does not exist in subjects.json; and a dosing "
                   "record for an S-010 Week 24 visit that was never recorded. Neither can be "
                   "assessed against a protocol schedule.")

    # -- A10: out-of-range values ------------------------------------------
    def out_of_range(self):
        record = self.find_vitals("S-013", "V5")
        record["weight"] = "8.16"
        dose_id = self.mark_dose_unassessable("S-013", "V5")
        plant(layer="A", code="A10 out-of-range value", file="vitals.json / dosing.json",
              records=[record["vitals_record_id"], dose_id], subject="S-013",
              verdict="not_assessable", routing="raise_site_query",
              note="weight = 8.16 kg for an adult -- a decimal slip of 81.6. A plausibility "
                   "bound (30-250 kg) must reject this rather than computing a 41 mg expected "
                   "dose and reporting the real 355 mg as a 767% overdose. State the bound in "
                   "the finding so the site can see what was rejected and why.",
              expect=[expectation("S-013", "V5", "not_assessable", "dose_deviation")])

        record = self.find_vitals("S-011", "SCR")
        record["weight"] = "1800"
        plant(layer="A", code="A10 out-of-range value", file="vitals.json",
              records=[record["vitals_record_id"]], subject="S-011",
              verdict="not_assessable", routing="raise_site_query",
              note="weight = 1800 in a kilogram field -- pounds in a kg field, or a typo. "
                   "Rejected by the same plausibility bound. S-011 never dosed, so no dose "
                   "assessment is blocked; this is a data quality finding only.")

    # -- A11: date format ambiguity, SITE-02 only ---------------------------
    def ambiguous_dates(self):
        # For the ambiguity to actually matter, the two readings must land on
        # OPPOSITE sides of the window. If both fall outside it, the verdict is
        # decidable without resolving the ambiguity and the record tests
        # nothing. Search for a date where the DD/MM reading is inside the
        # window and the MM/DD reading is not, rather than hand-picking one.
        planted = 0
        for subject_id, visit_id in [("S-006", "V4"), ("S-008", "V5"), ("S-005", "V5"),
                                     ("S-010", "V4"), ("S-008", "V4"), ("S-006", "V5")]:
            if planted >= 2:
                break
            found = self._straddling_ambiguous_date(subject_id, visit_id)
            if found is None:
                continue
            true_date, swapped, opens, closes = found
            record = self.find_visit(subject_id, visit_id)
            original = record["visit_date"]
            written = f"{true_date.day:02d}/{true_date.month:02d}/{true_date.year}"
            record["visit_date"] = written
            self.unassessable.add(record["visit_record_id"])
            planted += 1
            plant(layer="A", code="A11 ambiguous date format", file="visits.json",
                  records=[record["visit_record_id"]], subject=subject_id,
                  verdict="not_assessable", routing="raise_site_query",
                  note=f"visit_date written '{written}' (true value {original}). SITE-02 uses "
                       f"DD/MM/YYYY, but the file carries no locale declaration and both "
                       f"readings are real dates. Read DD/MM it is "
                       f"{true_date.isoformat()}, INSIDE the "
                       f"{opens.isoformat()}-{closes.isoformat()} window; read MM/DD it is "
                       f"{swapped.isoformat()}, outside it. The two readings give opposite "
                       f"verdicts, so picking a locale and proceeding is exactly how a "
                       f"confident wrong answer gets produced.",
              expect=[expectation(subject_id, visit_id, "not_assessable", "out_of_window_visit")])

    def _straddling_ambiguous_date(self, subject_id, visit_id):
        """Find a day in the visit window whose day/month swap lands outside it."""
        row = [r for r in ROSTER if r["subject_id"] == subject_id][0]
        anchor = date.fromisoformat(row["anchor"])
        entry = [v for v in schedule_for(row["version"]) if v["visit_id"] == visit_id][0]
        target = target_date(anchor, entry["target_day"])
        opens = target - timedelta(days=entry["window_before"])
        closes = target + timedelta(days=entry["window_after"])

        day = opens
        while day <= closes:
            # Both components must be <= 12 or only one reading is valid and
            # there is no ambiguity to test.
            if day.day <= 12 and day.month <= 12:
                try:
                    swapped = date(day.year, day.day, day.month)
                except ValueError:
                    swapped = None
                if swapped and swapped != day and not (opens <= swapped <= closes):
                    return day, swapped, opens, closes
            day += timedelta(days=1)
        return None

    def run(self):
        self.partial_dates()
        self.sentinels()
        self.id_drift()
        self.unit_drift()
        self.duplicates()
        self.free_text()
        self.referential_gaps()
        self.out_of_range()
        self.ambiguous_dates()
        # Background noise goes on last, over the top of the targeted damage,
        # so it can be told which records not to touch.
        self._scatter_partial_dates()


# --------------------------------------------------------------------------
# Static traps that need no record mutation
# --------------------------------------------------------------------------

def plant_static_traps():
    plant(layer="B", code="B1 derived schedule", file="protocol.json",
          records=["PROTO-001 dosing block"], subject="(all)", verdict="compliant",
          routing="none - arithmetic requirement",
          note="The protocol states 5 mg/kg, every 4 weeks, total_doses 8 over a 32-week "
               "treatment period. There is no per-administration dose field and no interval-in-"
               "days field. Both must be derived: 32 weeks / 8 doses = 28 days, and the expected "
               "dose is 5 mg/kg x the weight as of that visit.")

    plant(layer="B", code="B6 unit trap (weight in lb)", file="vitals.json / dosing.json",
          records=["S-007 all vitals"], subject="S-007", verdict="compliant",
          routing="none - S-007's dosing is correct",
          note="SITE-03 records weight in lb. S-007 weighs 180 lb = 81.6 kg, so 5 mg/kg expects "
               "408 mg, and 408 mg was administered -- compliant. Read as kg the expectation "
               "becomes 900 mg and a correct dose looks like a 55% underdose. This is the "
               "highest-consequence false positive in the dataset.",
              expect=[expectation("S-007", "V1", "compliant")])

    plant(layer="B", code="B7 per-visit windows differ", file="protocol.json",
          records=["visit_schedule"], subject="(all)", verdict="compliant",
          routing="none - arithmetic requirement",
          note="Day 1 is +/-0, Week 4/8/12 are +/-3 (+/-5 under v2.0), Week 24 is +/-7 and EOT is "
               "+/-14. A single global tolerance produces wrong answers at both ends: it "
               "fabricates deviations at EOT and misses them at Day 1.")

    plant(layer="A", code="A9 late data entry", file="all files", records=["SITE-03 records"],
          subject="SITE-03", verdict="compliant",
          routing="data quality finding -> open_capa (site level)",
          note="SITE-03's entered_date runs 30-90 days after the event; SITE-01 and SITE-02 are "
               "within a week. Late entry lowers reliability and is a monitoring signal in its "
               "own right, but it is NOT a protocol deviation and must not be filed as one.")

    plant(layer="A", code="Site-level quality pattern", file="(derived)",
          records=["SITE-03"], subject="SITE-03", verdict="not_assessable",
          routing="open_capa (data quality) - a SEPARATE finding family from deviations",
          note="SITE-03 carries A3, A4, A9 and most of A2, and accounts for the majority of "
               "unassessable records. 'SITE-03 accounts for N% of unassessable records' is a "
               "DATA QUALITY finding, not a protocol deviation. Conflating the two inflates the "
               "deviation rate the sponsor reports to the regulator.")


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def render_data_traps(builder: Builder, layer_a: LayerA, stats: dict) -> str:
    lines = [
        "# DATA_TRAPS — ground truth for the synthetic dataset",
        "",
        "Generated by `scripts/generate_data.py` (seed "
        f"`{SEED}`). Do not edit by hand: this file is rendered from the same",
        "registry the data is built from, so it cannot drift.",
        "",
        "**Three verdicts, not two.** `deviation` — assessed and departs from the protocol.",
        "`compliant` — assessed and conforms. `not_assessable` — *cannot* be assessed because",
        "the source data is incomplete or ambiguous. A system that emits only the first two",
        "silently converts missing data into false negatives. `not_assessable` routes to a site",
        "query, never to a deviation record.",
        "",
        "Two finding families, kept separate: **protocol deviations** (against the subject and",
        "the protocol) and **data quality findings** (against the site). Filing a data entry",
        "problem as a deviation inflates the deviation rate the sponsor reports.",
        "",
        "---",
        "",
        "## Layer B — domain traps",
        "",
    ]

    def section(layer_key):
        rows = [issue for issue in ISSUES if issue["layer"] == layer_key]
        rows.sort(key=lambda item: item["code"])
        out = []
        for issue in rows:
            out.append(f"### {issue['code']} — {issue['subject']}")
            out.append("")
            out.append(f"- **File:** `{issue['file']}`")
            out.append(f"- **Records:** {', '.join(str(r) for r in issue['records'])}")
            out.append(f"- **Expected verdict:** `{issue['verdict']}`")
            out.append(f"- **Expected routing:** {issue['routing']}")
            out.append("")
            out.append(issue["note"])
            out.append("")
        return out

    lines += section("B")
    lines += ["---", "", "## Layer A — pervasive deficiencies", ""]
    lines += section("A")

    lines += [
        "---",
        "",
        "## Fully clean subjects",
        "",
        "A dataset where everything is a finding proves nothing. These subjects are fully",
        "compliant and fully assessable, with no Layer A damage:",
        "",
        "".join(f"- `{s}`\n" for s in CLEAN_SUBJECTS),
        "## Generation summary",
        "",
        "```",
        stats["summary_text"],
        "```",
        "",
    ]
    return "\n".join(lines)


def verify_registry(data_dir: Path) -> list[str]:
    """Check the registry's claims against the files that were actually written.

    DATA_TRAPS is rendered from the same registry the data is built from, which
    means it cannot drift -- but it also means it cannot *disagree*. Plant the
    wrong thing and it documents the wrong thing confidently.

    This pass re-opens the written JSON and checks that every record ID the
    registry names exists, and every subject it names is in the enrolment file.
    It does not make the ground truth independent, but it stops the registry
    asserting something the data does not contain.
    """
    problems: list[str] = []

    def load(name):
        return json.loads((data_dir / name).read_text(encoding="utf-8"))

    known_ids: set[str] = set()
    for name, field in (("visits.json", "visit_record_id"),
                        ("dosing.json", "dosing_record_id"),
                        ("vitals.json", "vitals_record_id"),
                        ("deviation_log.json", "deviation_id")):
        known_ids.update(str(row[field]) for row in load(name))

    subjects = {row["subject_id"] for row in load("subjects.json")}
    id_pattern = re.compile(r"\b(?:VR|DR|VT|DEV)-\d{4}\b")

    for issue in ISSUES:
        for reference in issue["records"]:
            for record_id in id_pattern.findall(str(reference)):
                if record_id not in known_ids:
                    problems.append(
                        f"{issue['code']}: names record {record_id}, which is not in "
                        f"any generated file"
                    )
        for expectation in issue["expect"]:
            subject_id = expectation["subject_id"]
            if subject_id and subject_id not in subjects:
                problems.append(
                    f"{issue['code']}: expects a verdict for {subject_id}, who is not "
                    f"in subjects.json"
                )
            if expectation["verdict"] not in ("deviation", "compliant", "not_assessable"):
                problems.append(
                    f"{issue['code']}: unknown expected verdict "
                    f"{expectation['verdict']!r}"
                )

    # Every subject declared clean must actually be free of Layer A damage.
    for name, id_field in (("visits.json", "visit_record_id"),
                           ("vitals.json", "vitals_record_id"),
                           ("dosing.json", "dosing_record_id")):
        for row in load(name):
            if row.get("subject_id") not in CLEAN_SUBJECTS:
                continue
            for key, value in row.items():
                if key.endswith("date") and isinstance(value, str):
                    try:
                        date.fromisoformat(value)
                    except ValueError:
                        problems.append(
                            f"clean subject {row['subject_id']}: {row[id_field]} has "
                            f"{key}={value!r}, which is not a full ISO date"
                        )
            if name == "vitals.json" and "weight_unit" not in row:
                problems.append(
                    f"clean subject {row['subject_id']}: {row[id_field]} has no "
                    f"weight_unit"
                )
    return problems


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


# --------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------

def build_summary(builder: Builder, layer_a: LayerA, protocol) -> dict:
    # An assessable unit is a question the system has to answer: "was this
    # visit inside its window?" and "was this dose the expected mg?". Counting
    # visits alone would hide every weight problem, which is most of SITE-03's
    # damage and the whole point of the lb/sentinel/plausibility work.
    site_by_subject = {row["subject_id"]: row["site_id"] for row in ROSTER}
    for alias in LayerA.S007_ALIASES:
        site_by_subject[alias] = "SITE-03"

    units = []
    for record in builder.visits:
        units.append((
            record.get("site_id") or site_by_subject.get(record["subject_id"], "(unknown)"),
            "visit",
            record["visit_record_id"] in layer_a.unassessable,
        ))
    for record in builder.dosing:
        units.append((
            site_by_subject.get(record["subject_id"], "(unknown)"),
            "dose",
            record["dosing_record_id"] in layer_a.unassessable_doses,
        ))

    total_units = len(units)
    unassessable = sum(1 for _, _, bad in units if bad)
    rate = unassessable / total_units * 100 if total_units else 0

    by_site = defaultdict(lambda: {"units": 0, "unassessable": 0})
    for site, _kind, bad in units:
        by_site[site]["units"] += 1
        if bad:
            by_site[site]["unassessable"] += 1

    issue_counts = Counter(issue["code"].split()[0] for issue in ISSUES)
    verdicts = Counter(issue["verdict"] for issue in ISSUES)

    visit_units = sum(1 for _, kind, _ in units if kind == "visit")
    bad_visits = sum(1 for _, kind, bad in units if kind == "visit" and bad)
    bad_doses = unassessable - bad_visits

    lines = [
        "RECORD COUNTS",
        f"  protocol.json        {len(protocol)} versions (v1.0, v2.0)",
        f"  subjects.json        {len(builder.subjects)}",
        f"  visits.json          {len(builder.visits)}",
        f"  dosing.json          {len(builder.dosing)}",
        f"  vitals.json          {len(builder.vitals)}",
        f"  deviation_log.json   {len(builder.deviation_log)}",
        "",
        "ASSESSABILITY (unit = one question the system must answer)",
        f"  visit-window units   {visit_units}   of which not_assessable {bad_visits}",
        f"  dose units           {total_units - visit_units}   of which not_assessable {bad_doses}",
        f"  total units          {total_units}",
        f"  expected not_assessable {unassessable}  ({rate:.1f}%)",
        f"  target band          5.0% - 25.0%   [{'OK' if 5 <= rate <= 25 else 'OUT OF BAND'}]",
        "",
        "WHY THOSE UNITS ARE NOT ASSESSABLE",
        "  partial visit date (day unknown)     - cannot be placed in a +/-3 day window",
        "  ambiguous DD/MM vs MM/DD date        - two valid readings, opposite verdicts",
        "  duplicate visit with date drift      - which record is authoritative is a site question",
        "  missing / sentinel / implausible wt  - 5 mg/kg cannot be computed",
        "  missing weight unit                  - assuming kg is the lb trap in reverse",
        "  orphan or unknown subject_id         - nothing to assess against",
        "",
        "SITE BREAKDOWN",
    ]
    for site in sorted(by_site):
        row = by_site[site]
        share = row["unassessable"] / unassessable * 100 if unassessable else 0
        lines.append(
            f"  {site:<12} units {row['units']:>3}   unassessable {row['unassessable']:>3}"
            f"   ({share:.0f}% of all unassessable)"
        )
    lines += [
        "",
        "PLANTED ISSUES BY CODE",
    ]
    for code in sorted(issue_counts):
        lines.append(f"  {code:<6} {issue_counts[code]}")
    lines += [
        "",
        "EXPECTED VERDICTS ACROSS PLANTED ISSUES",
    ]
    for verdict in sorted(verdicts):
        lines.append(f"  {verdict:<16} {verdicts[verdict]}")
    lines += [
        "",
        f"FULLY CLEAN SUBJECTS   {', '.join(CLEAN_SUBJECTS)}",
    ]

    return {"summary_text": "\n".join(lines), "rate": rate,
            "total_units": total_units, "unassessable": unassessable}


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> None:
    ISSUES.clear()
    protocol = build_protocol()

    builder = Builder(SEED)
    builder.build_subjects()
    for row in ROSTER:
        builder.build_subject_records(row)
    builder.build_deviation_log()

    plant_static_traps()

    layer_a = LayerA(builder)
    layer_a.run()

    stats = build_summary(builder, layer_a, protocol)

    write_json(DATA / "protocol.json", protocol)
    write_json(DATA / "subjects.json", builder.subjects)
    write_json(DATA / "visits.json", builder.visits)
    write_json(DATA / "dosing.json", builder.dosing)
    write_json(DATA / "vitals.json", builder.vitals)
    write_json(DATA / "deviation_log.json", builder.deviation_log)

    DOCS.mkdir(parents=True, exist_ok=True)
    (DOCS / "DATA_TRAPS.md").write_text(
        render_data_traps(builder, layer_a, stats), encoding="utf-8"
    )
    # The same ground truth, machine-checkable. Prose can only be diffed by a
    # human reading carefully, which is exactly the check that keeps slipping.
    write_json(DOCS / "data_traps.json", {
        "seed": SEED,
        "clean_subjects": CLEAN_SUBJECTS,
        "not_assessable_rate": round(stats["rate"], 2),
        "issues": ISSUES,
    })

    print(stats["summary_text"])

    problems = verify_registry(DATA)
    print()
    print("GROUND TRUTH VERIFICATION (registry claims re-checked against the "
          "written files)")
    if problems:
        for problem in problems:
            print(f"  MISMATCH  {problem}")
        raise SystemExit(f"\n{len(problems)} registry claim(s) do not match the "
                         f"generated data.")
    print(f"  {len(ISSUES)} planted issues, "
          f"{sum(len(i['expect']) for i in ISSUES)} expectations, all consistent "
          f"with the data on disk.")

    print()
    print(f"Wrote 6 files to {DATA} and docs/DATA_TRAPS.md")


if __name__ == "__main__":
    main()
