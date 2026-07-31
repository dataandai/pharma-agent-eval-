"""Phase 4: the three behaviours the brief names, plus the detector contract.

Deliberately short. The exhaustive check is scripts/diff_findings.py, which
diffs every detector against docs/data_traps.json; duplicating it here as
assertions would be the same check written twice.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.detectors import run_all
from src.findings import Family, ProposedAction
from src.study import Study
from src.verdicts import Verdict

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def findings():
    return run_all(Study.load(ROOT / "data"))


def for_subject(findings, subject_id, visit_id=None, detector=None):
    return [f for f in findings
            if f.subject_id == subject_id
            and (visit_id is None or f.visit_id == visit_id)
            and (detector is None or f.detector == detector)]


# --------------------------------------------------------------------------
# The three the brief names
# --------------------------------------------------------------------------

def test_trap_3_screen_failure_produces_no_compliance_findings(findings):
    """S-011 was screened, has laboratory data, failed eligibility and was
    never dosed. Protocol compliance does not apply to someone who was never
    enrolled; a deviation filed against them is a false positive."""
    compliance = [f for f in for_subject(findings, "S-011")
                  if f.family is Family.PROTOCOL_DEVIATION]
    assert compliance == []


def test_trap_4_an_already_logged_deviation_is_not_refiled(findings):
    """S-002's Week 8 is genuinely out of window AND already in
    deviation_log.json. Filing it again double-reports and inflates the
    deviation rate the sponsor reports."""
    found = for_subject(findings, "S-002", "V3", "out_of_window_visit")
    assert len(found) == 1
    finding = found[0]
    assert finding.verdict is Verdict.DEVIATION        # still true
    assert finding.is_suppressed                        # but not filed again
    assert finding.suppressed_by == "deviation_log:DEV-0001"
    assert finding.proposed_actions == ()


def test_trap_8_immediate_hazard_is_documented_without_a_capa(findings):
    """E6(R3) 2.5.3: a deviation made to eliminate an immediate hazard is
    permitted. It is documented; it does NOT warrant a corrective action
    against a site that did exactly the right thing."""
    found = for_subject(findings, "S-005", "V3", "dose_deviation")
    assert len(found) == 1
    finding = found[0]

    assert finding.verdict is Verdict.DEVIATION
    assert ProposedAction.LOG_DEVIATION in finding.proposed_actions
    assert ProposedAction.OPEN_CAPA not in finding.proposed_actions
    assert "2.5.3" in finding.calculation


def test_the_hazard_verdict_is_not_parsed_out_of_the_free_text(findings):
    """The coded dose_status drives the routing. The clinical note is quoted as
    evidence for the investigator, never read to reach a verdict."""
    finding = for_subject(findings, "S-005", "V3", "dose_deviation")[0]
    assert any("neutropenia" in e.lower() for e in finding.evidence)
    # The reasoning belongs to the reviewer, not to the filed record.
    assert "deliberately not parsed" in finding.rationale
    assert "neutropenia" not in finding.calculation.lower()


# --------------------------------------------------------------------------
# The version pair, end to end through the detectors
# --------------------------------------------------------------------------

def test_the_version_pair_survives_the_whole_pipeline(findings):
    s004 = for_subject(findings, "S-004", "V2", "out_of_window_visit")
    s009 = for_subject(findings, "S-009", "V2", "out_of_window_visit")
    assert len(s004) == 1 and s004[0].verdict is Verdict.DEVIATION
    assert s009 == []          # compliant, so no finding at all


# --------------------------------------------------------------------------
# The systemic pattern subsumes rather than repeats
# --------------------------------------------------------------------------

def test_systemic_pattern_replaces_the_individual_reports(findings):
    patterns = [f for f in findings if f.detector == "systemic_pattern"]
    assert len(patterns) == 1
    pattern = patterns[0]

    assert pattern.site_id == "SITE-02" and pattern.visit_id == "V2"
    assert len(pattern.subsumes) == 4
    assert ProposedAction.PROPOSE_PROTOCOL_AMENDMENT in pattern.proposed_actions
    assert ProposedAction.OPEN_CAPA in pattern.proposed_actions

    # The four it subsumes still exist and are still auditable, but none is filed.
    subsumed = [f for f in findings if f.finding_id in pattern.subsumes]
    assert len(subsumed) == 4
    assert all(f.is_suppressed and f.proposed_actions == () for f in subsumed)


# --------------------------------------------------------------------------
# The detector contract
# --------------------------------------------------------------------------

def test_every_deviation_carries_a_classification_proposal(findings):
    """E6(R3) places the review on the investigator, so the agent proposes and
    never decides."""
    for finding in findings:
        if finding.verdict is Verdict.DEVIATION and finding.family is Family.PROTOCOL_DEVIATION:
            assert finding.classification is not None, finding.finding_id
            assert finding.classification.is_proposal
            assert finding.classification.decided_by is None
            assert finding.classification.requires_investigator_review
            assert len(finding.classification.reasoning) > 40


def test_no_finding_uses_sponsor_house_terminology(findings):
    """major / minor / critical are not E6(R3) categories."""
    for finding in findings:
        if finding.classification:
            assert finding.classification.proposed in ("important", "not important")


def test_data_quality_findings_never_propose_a_deviation_record(findings):
    """A data entry problem is not a protocol deviation. Filing it as one
    inflates the deviation rate the sponsor reports to the regulator."""
    for finding in findings:
        if finding.family is Family.DATA_QUALITY:
            assert ProposedAction.LOG_DEVIATION not in finding.proposed_actions


def test_not_assessable_findings_never_route_to_the_deviation_log(findings):
    for finding in findings:
        if finding.verdict is Verdict.NOT_ASSESSABLE:
            assert ProposedAction.LOG_DEVIATION not in finding.proposed_actions


def test_every_unassessable_visit_occasion_gets_exactly_one_query(findings):
    """A visit record and its dose record are different rows describing one
    event. Three detectors noticing the same bad date must not send the site
    three questions about it -- but the event must still be asked about once."""
    occasions: dict[str, list] = {}
    for finding in findings:
        if finding.verdict is not Verdict.NOT_ASSESSABLE:
            continue
        key = f"{finding.subject_id}/{finding.visit_id}"
        occasions.setdefault(key, []).append(finding)

    for key, group in occasions.items():
        queries = [f for f in group
                   if ProposedAction.RAISE_SITE_QUERY in f.proposed_actions]
        assert len(queries) == 1, (
            f"{key} produced {len(queries)} site queries: "
            f"{[f.finding_id for f in queries]}"
        )
        for finding in group:
            if finding not in queries:
                assert queries[0].finding_id in finding.rationale


def test_site_level_observations_carry_no_verdict(findings):
    """A verdict states whether one record complied with the protocol. "SITE-03
    enters data late" makes no such claim, so it has none -- otherwise the rule
    "compliance is the absence of a finding" is quietly contradicted by a stream
    of COMPLIANT findings."""
    observations = [f for f in findings if f.detector == "site_data_quality"]
    assert observations
    assert all(f.verdict is None for f in observations)
    assert all(f.verdict is not Verdict.COMPLIANT for f in findings)


def test_findings_that_depend_on_a_threshold_name_it(findings):
    """Six invented numbers drive every verdict. A reader must be able to see
    which one drove this answer, and disagree with it precisely."""
    from src.thresholds import RATIONALE

    named = [f for f in findings if f.threshold_applied]
    assert named
    for finding in named:
        assert finding.threshold_applied in RATIONALE
        assert "illustrative threshold" in finding.rationale


def test_findings_are_deterministic_and_uniquely_identified(findings):
    ids = [f.finding_id for f in findings]
    assert len(ids) == len(set(ids))
    assert run_all(Study.load(ROOT / "data")) == findings


def test_every_calculation_names_a_real_record_or_date(findings):
    """The calculation string is what the agent reads out, so the model never
    has to produce a figure."""
    for finding in findings:
        assert len(finding.calculation) > 40
        assert any(token in finding.calculation
                   for token in ("VR-", "DR-", "VT-", "20", "SITE-"))


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------

def test_the_dataset_diff_reports_no_mismatches():
    """Runs every detector over the full dataset and diffs the output against
    the planted ground truth."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "diff_findings.py")],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout
    assert "no mismatches" in result.stdout
