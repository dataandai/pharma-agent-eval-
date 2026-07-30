"""Phase 4 gate: run every detector over the full dataset and diff the output
against the planted ground truth in docs/data_traps.json.

Reports three things, and exits non-zero on any of them:

- **Missed**  an expectation the detectors did not produce.
- **Wrong**   an expectation matched by a finding with the wrong verdict,
              routing, or suppression state.
- **Unexplained** a reportable deviation the ground truth does not account for.
  This is the direction that catches the interesting bugs: a detector firing on
  something nobody planted usually means an accidental defect in the data, not
  a clever find.

Run:  python scripts/diff_findings.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.detectors import run_all           # noqa: E402
from src.findings import Family             # noqa: E402
from src.study import Study                 # noqa: E402
from src.verdicts import Verdict            # noqa: E402


def load_expectations() -> list[dict]:
    payload = json.loads((ROOT / "docs" / "data_traps.json").read_text(encoding="utf-8"))
    rows = []
    for issue in payload["issues"]:
        for expectation in issue["expect"]:
            rows.append({**expectation, "code": issue["code"], "layer": issue["layer"]})
    return rows


def matches(finding, expectation) -> bool:
    if expectation["subject_id"] != finding.subject_id:
        return False
    if expectation["visit_id"] and expectation["visit_id"] != finding.visit_id:
        return False
    if expectation["detector"] and expectation["detector"] != finding.detector:
        return False
    return True


def main() -> int:
    study = Study.load(ROOT / "data")
    findings = run_all(study)
    expectations = load_expectations()

    missed: list[dict] = []
    wrong: list[str] = []
    accounted: set[str] = set()

    for expectation in expectations:
        candidates = [f for f in findings if matches(f, expectation)]

        # "compliant" means: the detectors must NOT raise a deviation here.
        if expectation["verdict"] == "compliant":
            offending = [f for f in candidates
                         if f.verdict is Verdict.DEVIATION and f.is_reportable]
            if offending:
                wrong.append(
                    f"{expectation['code']}: expected NO deviation for "
                    f"{expectation['subject_id']}/{expectation['visit_id'] or 'any visit'}, "
                    f"got {', '.join(f'{f.finding_id} {f.detector}' for f in offending)}"
                )
            accounted.update(f.finding_id for f in candidates)
            continue

        hit = [f for f in candidates if f.verdict.value == expectation["verdict"]]
        if not hit:
            missed.append(expectation)
            continue

        finding = hit[0]
        accounted.add(finding.finding_id)

        if expectation["suppressed"] and not finding.is_suppressed:
            wrong.append(f"{expectation['code']}: {finding.finding_id} should be suppressed "
                         f"but has actions {[a.value for a in finding.proposed_actions]}")
        if expectation["suppressed"] is None and finding.is_suppressed:
            wrong.append(f"{expectation['code']}: {finding.finding_id} is suppressed by "
                         f"{finding.suppressed_by} but the ground truth expects it filed")

        actions = {a.value for a in finding.proposed_actions}
        for required in expectation["actions"]:
            if required not in actions:
                wrong.append(f"{expectation['code']}: {finding.finding_id} is missing "
                             f"action {required!r} (has {sorted(actions)})")
        for forbidden in expectation["forbidden_actions"]:
            if forbidden in actions:
                wrong.append(f"{expectation['code']}: {finding.finding_id} must NOT propose "
                             f"{forbidden!r}")
        for citation in expectation["must_cite"]:
            if citation not in finding.calculation:
                wrong.append(f"{expectation['code']}: {finding.finding_id} must cite "
                             f"{citation!r} in its calculation")

    # The other direction: reportable deviations nobody planted.
    unexplained = [
        f for f in findings
        if f.family is Family.PROTOCOL_DEVIATION
        and f.verdict is Verdict.DEVIATION
        and f.is_reportable
        and f.finding_id not in accounted
        and f.detector != "systemic_pattern"
    ]

    # ---- report ----------------------------------------------------------
    print("=" * 78)
    print("DETECTOR OUTPUT")
    print("=" * 78)
    by_detector: dict[tuple[str, str], int] = Counter(
        (f.detector, f.verdict.value) for f in findings
    )
    for (detector, verdict), count in sorted(by_detector.items()):
        print(f"  {detector:<24} {verdict:<16} {count}")
    print(f"  {'TOTAL':<24} {'':<16} {len(findings)}")

    reportable = [f for f in findings if f.is_reportable]
    suppressed = [f for f in findings if f.is_suppressed]
    print(f"\n  reportable {len(reportable)}   suppressed {len(suppressed)}")

    families = Counter(f.family.value for f in findings)
    print(f"  protocol_deviation {families['protocol_deviation']}   "
          f"data_quality {families['data_quality']}")

    print()
    print("=" * 78)
    print(f"DIFF AGAINST docs/data_traps.json  ({len(expectations)} expectations)")
    print("=" * 78)

    if not missed and not wrong and not unexplained:
        print("  no mismatches")
    if missed:
        print(f"\n  MISSED ({len(missed)}) -- planted but not detected:")
        for expectation in missed:
            print(f"    {expectation['code']}: expected {expectation['verdict']} for "
                  f"{expectation['subject_id']}/{expectation['visit_id']} "
                  f"from {expectation['detector'] or 'any detector'}")
    if wrong:
        print(f"\n  WRONG ({len(wrong)}) -- detected but not as specified:")
        for line in wrong:
            print(f"    {line}")
    if unexplained:
        print(f"\n  UNEXPLAINED ({len(unexplained)}) -- filed deviations the ground truth "
              f"does not account for:")
        for finding in unexplained:
            print(f"    {finding.finding_id} {finding.detector} "
                  f"{finding.subject_id}/{finding.visit_id}")
            print(f"      {finding.calculation[:160]}")

    print()
    print("=" * 78)
    print("WHAT WOULD BE FILED")
    print("=" * 78)
    actions: dict[str, int] = Counter()
    for finding in reportable:
        for action in finding.proposed_actions:
            actions[action.value] += 1
    for action, count in sorted(actions.items()):
        print(f"  {action:<32} {count}")

    unresolved = len(missed) + len(wrong) + len(unexplained)
    print(f"\n{'PASS' if unresolved == 0 else f'FAIL: {unresolved} mismatch(es)'}")
    return 0 if unresolved == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
