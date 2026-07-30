"""The detectors. Pure functions over a loaded Study, returning Findings.

Two rules run through all of them:

**Compliance is the absence of a finding.** A detector emits a Finding only for
`deviation` and `not_assessable`. Emitting one per compliant visit would bury
the four that matter under ninety that do not.

**Never parse meaning out of free text to drive a verdict.** Coded fields decide
routing; prose is quoted as evidence for a human. This bites hardest on the
withheld dose, where the clinical rationale sits in a `reason` string: the
*coded* `dose_status` is what suppresses the CAPA, and the note is quoted so the
investigator can confirm it.

Every classification is a proposal with its reasoning attached. E6(R3) places
the review on the investigator.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Iterable

from src.dates import days_between
from src.dosing import normalise_dose
from src.findings import (
    IMPORTANT,
    NOT_IMPORTANT,
    ClassificationProposal,
    Family,
    Finding,
    ProposedAction,
    assign_ids,
)
from src.protocol import Schedule, assess_timing, expected_schedule, governing_version
from src.quantities import QuantityError, parse_quantity
from src.study import Study
from src.verdicts import Verdict

# Illustrative thresholds. A qualified clinical team would set these; they are
# stated here so a reader can see exactly what drove a verdict.
DOSE_TOLERANCE = Decimal("0.10")            # +/-10% of expected is within tolerance
DOSE_IMPORTANT_THRESHOLD = Decimal("0.20")  # beyond +/-20%, propose "important"
SYSTEMIC_MIN_SUBJECTS = 3                   # a pattern needs at least this many
SYSTEMIC_MIN_SHARE = Decimal("0.5")         # ...and this share of the site's subjects
LATE_ENTRY_DAYS = 14                        # entry lag beyond this is a monitoring signal


def _schedules(study: Study) -> dict[str, Schedule]:
    return {s.subject_id: expected_schedule(s, study.catalogue)
            for s in study.enrolled_subjects()}


def _site_of(study: Study, subject_id: str | None) -> str | None:
    subject = study.subjects.get(subject_id or "")
    return subject.site_id if subject else None


# --------------------------------------------------------------------------
# 1. missed_visit
# --------------------------------------------------------------------------

def missed_visit(study: Study) -> list[Finding]:
    """A scheduled visit with no record and a closed window.

    Absence is only a finding once the window has shut -- before that the visit
    is simply not due yet. And absence of a *visit* record while a *dose* record
    exists is a missing record, not a missed visit: the visit demonstrably
    happened.
    """
    findings: list[Finding] = []
    for subject_id, schedule in _schedules(study).items():
        for scheduled in schedule.visits:
            if study.visit_records(subject_id, scheduled.visit_id):
                continue
            if scheduled.closes >= study.as_of:
                continue

            doses = study.dose_records(subject_id, scheduled.visit_id)
            site = _site_of(study, subject_id)

            if doses:
                findings.append(Finding(
                    detector="missed_visit",
                    family=Family.DATA_QUALITY,
                    verdict=Verdict.NOT_ASSESSABLE,
                    subject_id=subject_id, site_id=site, visit_id=scheduled.visit_id,
                    record_ids=tuple(d.record_id for d in doses),
                    calculation=(
                        f"No visit record exists for {subject_id} {scheduled.label}, but dose "
                        f"record {doses[0].record_id} was administered on "
                        f"{doses[0].dose_date.describe()}. The visit occurred; the visit record "
                        f"is missing. Whether it fell inside the window "
                        f"({scheduled.opens.isoformat()} to {scheduled.closes.isoformat()}) "
                        f"cannot be assessed without it."
                    ),
                    proposed_actions=(ProposedAction.RAISE_SITE_QUERY,),
                ))
                continue

            requires_dose = "dose" in scheduled.required_assessments
            findings.append(Finding(
                detector="missed_visit",
                family=Family.PROTOCOL_DEVIATION,
                verdict=Verdict.DEVIATION,
                subject_id=subject_id, site_id=site, visit_id=scheduled.visit_id,
                record_ids=(),
                calculation=(
                    f"{scheduled.describe()}. No visit record exists for {subject_id} and the "
                    f"window closed on {scheduled.closes.isoformat()}, "
                    f"{(study.as_of - scheduled.closes).days} days ago."
                    + (f" The visit required a dose, so a scheduled administration was also "
                       f"missed." if requires_dose else "")
                ),
                classification=ClassificationProposal(
                    proposed=IMPORTANT if requires_dose else NOT_IMPORTANT,
                    reasoning=(
                        "A missed dosing visit means the participant did not receive a "
                        "scheduled administration and the corresponding efficacy and safety "
                        "data were never collected, which may significantly affect both the "
                        "reliability of the study data and the participant's treatment."
                        if requires_dose else
                        "A single missed non-dosing visit leaves a gap in the data but does "
                        "not by itself affect participant safety or the reliability of the "
                        "primary endpoint. The investigator may take a different view."
                    ),
                ),
                proposed_actions=(ProposedAction.RAISE_SITE_QUERY,
                                  ProposedAction.LOG_DEVIATION),
            ))
    return findings


# --------------------------------------------------------------------------
# 2. out_of_window_visit
# --------------------------------------------------------------------------

def out_of_window_visit(study: Study) -> list[Finding]:
    """A visit performed outside target_day +/- window, judged three-valued.

    The window comes from the subject's *governing* protocol version, so two
    subjects with identical calendar lateness can reach opposite verdicts.
    """
    findings: list[Finding] = []
    for subject_id, schedule in _schedules(study).items():
        subject = study.subjects[subject_id]
        site = subject.site_id
        for scheduled in schedule.visits:
            records = study.visit_records(subject_id, scheduled.visit_id)
            if not records:
                continue

            if len(records) > 1:
                findings.append(_duplicate_finding(subject_id, site, scheduled, records))
                continue

            record = records[0]
            assessment = assess_timing(scheduled, record.visit_date)
            if assessment.verdict is Verdict.COMPLIANT:
                continue

            evidence = (f"Site comment: {record.comment!r}",) if record.comment else ()

            if assessment.verdict is Verdict.NOT_ASSESSABLE:
                findings.append(Finding(
                    detector="out_of_window_visit",
                    family=Family.DATA_QUALITY,
                    verdict=Verdict.NOT_ASSESSABLE,
                    subject_id=subject_id, site_id=site, visit_id=scheduled.visit_id,
                    record_ids=(record.record_id,),
                    calculation=f"{assessment.calculation} (record {record.record_id})",
                    proposed_actions=(ProposedAction.RAISE_SITE_QUERY,),
                    evidence=evidence,
                ))
                continue

            resolved = governing_version(
                subject, study.catalogue, on_date=record.visit_date.earliest
            )
            already = study.logged_deviation(
                subject_id, scheduled.visit_id, "visit_out_of_window"
            )

            days = assessment.days_late
            finding = Finding(
                detector="out_of_window_visit",
                family=Family.PROTOCOL_DEVIATION,
                verdict=Verdict.DEVIATION,
                subject_id=subject_id, site_id=site, visit_id=scheduled.visit_id,
                record_ids=(record.record_id,),
                calculation=(
                    f"{assessment.calculation} (record {record.record_id}). "
                    f"{resolved.explanation}"
                ),
                classification=ClassificationProposal(
                    proposed=NOT_IMPORTANT,
                    reasoning=(
                        f"A visit {abs(days) if days is not None else 'a few'} day(s) outside "
                        f"a {scheduled.window_text} day window is a departure from the "
                        f"schedule, but on its own it is unlikely to significantly affect the "
                        f"reliability of the data or the participant's safety. If the same "
                        f"window is missed repeatedly the pattern, not the individual visit, "
                        f"is the finding."
                    ),
                ),
                proposed_actions=(ProposedAction.LOG_DEVIATION,),
                evidence=evidence,
            )

            if already is not None:
                # The 'existing credit memo' case. The deviation is real; it is
                # already on the record, and filing it again double-reports.
                finding = replace(
                    finding,
                    suppressed_by=f"deviation_log:{already.deviation_id}",
                    proposed_actions=(),
                    calculation=(
                        f"{finding.calculation} This deviation is already recorded as "
                        f"{already.deviation_id} (classified {already.classification!r} by "
                        f"the investigator on the existing log entry), so it must not be "
                        f"filed a second time."
                    ),
                )
            findings.append(finding)
    return findings


def _duplicate_finding(subject_id, site, scheduled, records) -> Finding:
    dates = ", ".join(f"{r.record_id} dated {r.visit_date.describe()} "
                      f"(entered {r.entered_date.describe()})" for r in records)
    return Finding(
        detector="out_of_window_visit",
        family=Family.DATA_QUALITY,
        verdict=Verdict.NOT_ASSESSABLE,
        subject_id=subject_id, site_id=site, visit_id=scheduled.visit_id,
        record_ids=tuple(r.record_id for r in records),
        calculation=(
            f"{len(records)} records exist for {subject_id} {scheduled.label}: {dates}. "
            f"Neither is voided, so which date is authoritative is a question for the site. "
            f"The visit must not be counted twice and a date must not be picked silently."
        ),
        proposed_actions=(ProposedAction.RAISE_SITE_QUERY,),
    )


# --------------------------------------------------------------------------
# 3. missing_assessment
# --------------------------------------------------------------------------

def missing_assessment(study: Study) -> list[Finding]:
    """A visit occurred but a required assessment is absent.

    Which assessments are required depends on the governing version: the ECG at
    Week 12 exists only under v2.0, so the identical record is a deviation for
    one subject and compliant for another.
    """
    findings: list[Finding] = []
    for subject_id, schedule in _schedules(study).items():
        subject = study.subjects[subject_id]
        for scheduled in schedule.visits:
            records = study.visit_records(subject_id, scheduled.visit_id)
            if len(records) != 1:
                continue   # absent or duplicated: handled elsewhere
            record = records[0]
            missing = tuple(a for a in scheduled.required_assessments
                            if a not in record.assessments_done)
            if not missing:
                continue

            version = schedule.version
            findings.append(Finding(
                detector="missing_assessment",
                family=Family.PROTOCOL_DEVIATION,
                verdict=Verdict.DEVIATION,
                subject_id=subject_id, site_id=subject.site_id,
                visit_id=scheduled.visit_id,
                record_ids=(record.record_id,),
                calculation=(
                    f"{version.label} requires "
                    f"{', '.join(scheduled.required_assessments)} at {scheduled.label}. "
                    f"Record {record.record_id} ({record.visit_date.describe()}) records "
                    f"{', '.join(record.assessments_done) or 'nothing'}. "
                    f"Missing: {', '.join(missing)}. "
                    f"{subject_id} consented under {version.label}, which is what makes "
                    f"{'this' if len(missing) == 1 else 'these'} required."
                ),
                classification=ClassificationProposal(
                    proposed=NOT_IMPORTANT,
                    reasoning=(
                        f"A single absent assessment leaves a gap in the data for this visit. "
                        f"Whether it rises to important depends on why the assessment was "
                        f"added -- {', '.join(missing)} was introduced by an amendment, and "
                        f"the investigator is better placed to judge its safety relevance "
                        f"than a threshold is."
                    ),
                ),
                proposed_actions=(ProposedAction.RAISE_SITE_QUERY,
                                  ProposedAction.LOG_DEVIATION),
            ))
    return findings


# --------------------------------------------------------------------------
# 4. dose_deviation
# --------------------------------------------------------------------------

def dose_deviation(study: Study) -> list[Finding]:
    """Administered dose against the expected 5 mg/kg for that visit's weight.

    The expected figure is derived, never read from a field, and it uses the
    weight as of that visit. Where the weight cannot be trusted the dose is not
    assessable -- it is not a deviation and it is not zero.
    """
    findings: list[Finding] = []
    schedules = _schedules(study)

    for subject_id in sorted(schedules):
        subject = study.subjects[subject_id]
        version = schedules[subject_id].version
        rule = version.dosing
        for record in sorted(study.doses_for(subject_id),
                             key=lambda r: r.record_id):
            if record.was_withheld:
                findings.append(_withheld_dose_finding(subject, record, rule))
                continue

            when = record.dose_date.exact
            if when is None:
                findings.append(Finding(
                    detector="dose_deviation", family=Family.DATA_QUALITY,
                    verdict=Verdict.NOT_ASSESSABLE,
                    subject_id=subject_id, site_id=subject.site_id,
                    visit_id=record.visit_id, record_ids=(record.record_id,),
                    calculation=(
                        f"Dose record {record.record_id} is dated "
                        f"{record.dose_date.describe()}. Without a precise date the weight as "
                        f"of that day cannot be resolved, so the expected dose cannot be "
                        f"computed."
                    ),
                    proposed_actions=(ProposedAction.RAISE_SITE_QUERY,),
                ))
                continue

            expected = normalise_dose(
                rule["value"], rule["unit"], "mg", subject_id, when, source=study.weights,
            )
            if not expected.ok:
                findings.append(Finding(
                    detector="dose_deviation", family=Family.DATA_QUALITY,
                    verdict=Verdict.NOT_ASSESSABLE,
                    subject_id=subject_id, site_id=subject.site_id,
                    visit_id=record.visit_id, record_ids=(record.record_id,),
                    calculation=(
                        f"Dose record {record.record_id} records "
                        f"{record.raw_dose!r} {record.dose_unit!r} on {when.isoformat()}. "
                        f"{expected.explanation}"
                    ),
                    proposed_actions=(ProposedAction.RAISE_SITE_QUERY,),
                ))
                continue

            try:
                administered = parse_quantity(record.raw_dose, record.dose_unit)
            except QuantityError as exc:
                administered = None
                problem = str(exc)
            else:
                problem = "the administered dose is missing"

            if administered is None:
                findings.append(Finding(
                    detector="dose_deviation", family=Family.DATA_QUALITY,
                    verdict=Verdict.NOT_ASSESSABLE,
                    subject_id=subject_id, site_id=subject.site_id,
                    visit_id=record.visit_id, record_ids=(record.record_id,),
                    calculation=(
                        f"Expected {expected.dose} mg at {when.isoformat()} "
                        f"({expected.explanation}) but dose record {record.record_id} cannot "
                        f"be read: {problem}."
                    ),
                    proposed_actions=(ProposedAction.RAISE_SITE_QUERY,),
                ))
                continue

            given = administered.convert_to("mg").value
            difference = given - expected.dose
            share = abs(difference) / expected.dose if expected.dose else Decimal("0")
            if share <= DOSE_TOLERANCE:
                continue

            percent = (share * 100).quantize(Decimal("0.1"))
            direction = "below" if difference < 0 else "above"
            important = share > DOSE_IMPORTANT_THRESHOLD

            actions = [ProposedAction.LOG_DEVIATION]
            if important:
                actions.append(ProposedAction.ESCALATE_TO_MEDICAL_MONITOR)

            findings.append(Finding(
                detector="dose_deviation", family=Family.PROTOCOL_DEVIATION,
                verdict=Verdict.DEVIATION,
                subject_id=subject_id, site_id=subject.site_id,
                visit_id=record.visit_id, record_ids=(record.record_id,),
                calculation=(
                    f"{expected.explanation} Administered {given} mg "
                    f"(record {record.record_id}), which is {abs(difference)} mg "
                    f"({percent}%) {direction} the expected dose. Tolerance is "
                    f"+/-{DOSE_TOLERANCE * 100:.0f}%."
                ),
                classification=ClassificationProposal(
                    proposed=IMPORTANT if important else NOT_IMPORTANT,
                    reasoning=(
                        f"A {percent}% departure from the protocol dose exceeds the "
                        f"{DOSE_IMPORTANT_THRESHOLD * 100:.0f}% threshold at which "
                        f"under- or over-exposure may significantly affect both the "
                        f"reliability of the efficacy data and the participant's safety."
                        if important else
                        f"A {percent}% departure exceeds the +/-"
                        f"{DOSE_TOLERANCE * 100:.0f}% tolerance but stays within "
                        f"{DOSE_IMPORTANT_THRESHOLD * 100:.0f}%, so it is unlikely on its own "
                        f"to affect the reliability of the data or participant safety."
                    ),
                ),
                proposed_actions=tuple(actions),
                evidence=(f"Site reason: {record.reason!r}",) if record.reason else (),
            ))
    return findings


def _withheld_dose_finding(subject, record, rule) -> Finding:
    """A withheld dose, routed by the CODED status -- never by parsing the note.

    ICH E6(R3) 2.5.3 permits deviating from the protocol to eliminate an
    immediate hazard to the participant. It is still a deviation and is still
    documented, but the remediation is not a corrective action against a site
    that did the right thing. Whether 2.5.3 applies is a clinical judgement, so
    the source note is quoted for the investigator rather than parsed.
    """
    criteria = ", ".join(rule.get("hold_criteria", ())) or "none stated"
    return Finding(
        detector="dose_deviation",
        family=Family.PROTOCOL_DEVIATION,
        verdict=Verdict.DEVIATION,
        subject_id=subject.subject_id, site_id=subject.site_id,
        visit_id=record.visit_id, record_ids=(record.record_id,),
        calculation=(
            f"Dose record {record.record_id} on {record.dose_date.describe()} has "
            f"dose_status 'Withheld' and no administered amount. The protocol schedules a "
            f"dose at this visit and permits holding only for: {criteria}. A withheld dose "
            f"that does not meet a stated hold criterion is a departure from the protocol "
            f"and is therefore a deviation, which must be documented. "
            f"ICH E6(R3) Section 2.5.3 permits a deviation made to eliminate an immediate "
            f"hazard to the trial participant; where that applies, the deviation is "
            f"documented but does NOT warrant a corrective action against the site. A "
            f"clinical rationale is recorded on this record and is quoted below for the "
            f"investigator and medical monitor to confirm -- it is deliberately not parsed "
            f"to reach this verdict."
        ),
        classification=ClassificationProposal(
            proposed=IMPORTANT,
            reasoning=(
                "The participant did not receive a scheduled administration, which affects "
                "both the completeness of the exposure data and the participant's treatment. "
                "Note that proposing 'important' concerns the deviation's regulatory "
                "handling; it is not a criticism of the decision to withhold. If the "
                "investigator confirms this was done to eliminate an immediate hazard, "
                "E6(R3) 2.5.3 applies and no corrective action against the site follows."
            ),
        ),
        # Deliberately no OPEN_CAPA. A naive system opens a corrective action
        # against a site that did exactly the right thing.
        proposed_actions=(ProposedAction.LOG_DEVIATION,
                          ProposedAction.ESCALATE_TO_MEDICAL_MONITOR),
        evidence=(f"Source note on {record.record_id}: {record.reason!r}",)
        if record.reason else (),
    )


# --------------------------------------------------------------------------
# 5. consent_sequence
# --------------------------------------------------------------------------

def _consent_ordering_is_live(study: Study, subject, visit_id: str) -> bool:
    """True for visits the protocol places at or before Day 1.

    Those are the ones where an imprecise date leaves the consent-ordering
    question genuinely open.
    """
    try:
        version = study.catalogue.version(subject.protocol_version_consented)
    except KeyError:
        return True
    entry = version.visit(visit_id)
    if entry is None:
        return True
    if entry.get("screening_window") is not None:
        return True
    target = entry.get("target_day")
    return target is None or target <= 1


def consent_sequence(study: Study) -> list[Finding]:
    """A study procedure dated before informed consent."""
    findings: list[Finding] = []
    for subject_id in sorted(study.subjects):
        subject = study.subjects[subject_id]
        consent = subject.consent_date
        if not consent.known:
            continue
        for record in sorted(study.visits_for(subject_id), key=lambda r: r.record_id):
            span = days_between(consent, record.visit_date)
            if span is None:
                continue
            low, high = span
            if low >= 0:
                continue   # entirely on or after consent

            if high < 0:
                gap = -high if -high == -low else None
                findings.append(Finding(
                    detector="consent_sequence",
                    family=Family.PROTOCOL_DEVIATION,
                    verdict=Verdict.DEVIATION,
                    subject_id=subject_id, site_id=subject.site_id,
                    visit_id=record.visit_id, record_ids=(record.record_id,),
                    calculation=(
                        f"{subject_id} consented on {consent.describe()}. "
                        f"{record.visit_label} (record {record.record_id}) is dated "
                        f"{record.visit_date.describe()}, "
                        f"{gap if gap is not None else f'between {-high} and {-low}'} day(s) "
                        f"before consent. Assessments recorded at that visit: "
                        f"{', '.join(record.assessments_done) or 'none'}."
                    ),
                    classification=ClassificationProposal(
                        proposed=IMPORTANT,
                        reasoning=(
                            "A study procedure performed before informed consent affects the "
                            "participant's rights, which E6(R3) names explicitly in the "
                            "definition of an important deviation. It is reportable to the "
                            "IRB/EC in most sponsor frameworks and is not a matter of degree: "
                            "one day before consent is still before consent."
                        ),
                    ),
                    proposed_actions=(ProposedAction.LOG_DEVIATION,
                                      ProposedAction.ESCALATE_TO_MEDICAL_MONITOR,
                                      ProposedAction.OPEN_CAPA),
                ))
            elif _consent_ordering_is_live(study, subject, record.visit_id):
                # Only raise the question where it is genuinely open. For a
                # Week 24 visit the schedule already places it months after
                # consent, so an unreadable date there is a timing problem --
                # and out_of_window_visit has already queried the same record.
                # Two queries on one record for one root cause is noise.
                findings.append(Finding(
                    detector="consent_sequence",
                    family=Family.DATA_QUALITY,
                    verdict=Verdict.NOT_ASSESSABLE,
                    subject_id=subject_id, site_id=subject.site_id,
                    visit_id=record.visit_id, record_ids=(record.record_id,),
                    calculation=(
                        f"{subject_id} consented on {consent.describe()}. "
                        f"{record.visit_label} (record {record.record_id}) is dated "
                        f"{record.visit_date.describe()}, which could be up to {-low} day(s) "
                        f"before consent or up to {high} day(s) after it. Whether a procedure "
                        f"preceded consent cannot be answered from these dates."
                    ),
                    proposed_actions=(ProposedAction.RAISE_SITE_QUERY,),
                ))
    return findings


# --------------------------------------------------------------------------
# 6. eligibility_breach
# --------------------------------------------------------------------------

def eligibility_breach(study: Study) -> list[Finding]:
    """A subject enrolled or dosed despite a recorded eligibility failure.

    The inverse is not a finding: a screen failure who was never enrolled is
    simply a screen failure, and any compliance finding against them is a false
    positive.
    """
    findings: list[Finding] = []
    for subject_id in sorted(study.subjects):
        subject = study.subjects[subject_id]
        if not subject.screen_failure_reason:
            continue
        doses = study.doses_for(subject_id)
        if not (subject.is_enrolled or doses):
            continue   # correctly excluded -- nothing to report

        findings.append(Finding(
            detector="eligibility_breach",
            family=Family.PROTOCOL_DEVIATION,
            verdict=Verdict.DEVIATION,
            subject_id=subject_id, site_id=subject.site_id,
            record_ids=tuple(d.record_id for d in doses),
            calculation=(
                f"{subject_id} has a recorded screening failure "
                f"({subject.screen_failure_reason!r}) but is recorded as "
                f"{subject.enrollment_status!r}"
                + (f" with {len(doses)} dosing record(s)." if doses else ".")
                + " A subject who failed eligibility must not be enrolled or dosed."
            ),
            classification=ClassificationProposal(
                proposed=IMPORTANT,
                reasoning=(
                    "Enrolling a participant who does not meet eligibility criteria affects "
                    "their safety directly -- the criteria exist to exclude people for whom "
                    "the intervention carries unacceptable risk -- and it compromises the "
                    "analysis population."
                ),
            ),
            proposed_actions=(ProposedAction.LOG_DEVIATION,
                              ProposedAction.ESCALATE_TO_MEDICAL_MONITOR,
                              ProposedAction.OPEN_CAPA),
        ))
    return findings


# --------------------------------------------------------------------------
# 7. systemic_pattern
# --------------------------------------------------------------------------

def systemic_pattern(study: Study, findings: list[Finding]) -> tuple[list[Finding], list[Finding]]:
    """Aggregate repeated deviations into one amendment proposal.

    If most subjects at a site miss the same window by the same margin, the
    window is the problem and filing N deviation reports is not just noise -- it
    is the wrong remediation, and it hides the real finding.

    Returns (pattern findings, the original list with the subsumed ones marked
    suppressed). The individual findings are not deleted: they remain auditable
    and they are what the pattern is evidence of.
    """
    enrolled_per_site: dict[str, set[str]] = defaultdict(set)
    for subject in study.enrolled_subjects():
        enrolled_per_site[subject.site_id].add(subject.subject_id)

    grouped: dict[tuple[str, str], list[Finding]] = defaultdict(list)
    for finding in findings:
        if (finding.detector == "out_of_window_visit"
                and finding.verdict is Verdict.DEVIATION
                and not finding.is_suppressed
                and finding.site_id and finding.visit_id):
            grouped[(finding.site_id, finding.visit_id)].append(finding)

    patterns: list[Finding] = []
    suppressed_ids: set[str] = set()

    for (site_id, visit_id), group in sorted(grouped.items()):
        cohort = enrolled_per_site.get(site_id, set())
        if not cohort:
            continue
        share = Decimal(len(group)) / Decimal(len(cohort))
        if len(group) < SYSTEMIC_MIN_SUBJECTS or share < SYSTEMIC_MIN_SHARE:
            continue

        subjects = sorted(f.subject_id for f in group)
        label = group[0].visit_id
        scheduled = None
        for subject_id in subjects:
            scheduled = expected_schedule(
                study.subjects[subject_id], study.catalogue
            ).visit(visit_id)
            if scheduled:
                label = scheduled.label
                break

        patterns.append(Finding(
            detector="systemic_pattern",
            family=Family.PROTOCOL_DEVIATION,
            verdict=Verdict.DEVIATION,
            site_id=site_id, visit_id=visit_id,
            record_ids=tuple(rid for f in group for rid in f.record_ids),
            calculation=(
                f"{len(group)} of {len(cohort)} enrolled subjects at {site_id} "
                f"({share * 100:.0f}%) fell outside the {label} window: "
                f"{', '.join(subjects)}. "
                + " ".join(f.calculation.split(".")[0] + "." for f in group[:4])
                + f" A window missed by most of a site's subjects indicates the window is "
                  f"too tight for the site's operating conditions, not that {len(group)} "
                  f"separate errors occurred. Filing {len(group)} deviation reports would be "
                  f"the wrong remediation and would hide the real finding."
            ),
            classification=ClassificationProposal(
                proposed=IMPORTANT,
                reasoning=(
                    f"A deviation recurring across {share * 100:.0f}% of a site's subjects "
                    f"affects the reliability of that site's data as a whole rather than one "
                    f"visit. E6(R3) requires measures to prevent recurrence for important "
                    f"deviations, and documentation alone would not close this."
                ),
            ),
            proposed_actions=(ProposedAction.PROPOSE_PROTOCOL_AMENDMENT,
                              ProposedAction.OPEN_CAPA),
            subsumes=tuple(f.finding_id for f in group),
        ))
        suppressed_ids.update(f.finding_id for f in group)

    updated = [
        replace(f, suppressed_by="systemic_pattern", proposed_actions=())
        if f.finding_id in suppressed_ids else f
        for f in findings
    ]
    return patterns, updated


# --------------------------------------------------------------------------
# 8. unattributable_record
# --------------------------------------------------------------------------

def unattributable_record(study: Study) -> list[Finding]:
    """Records that cannot be tied to a subject. Escalate; never guess."""
    findings: list[Finding] = []

    for record in study.unattributable_visits:
        findings.append(Finding(
            detector="unattributable_record",
            family=Family.DATA_QUALITY,
            verdict=Verdict.NOT_ASSESSABLE,
            site_id=record.site_id, visit_id=record.visit_id,
            record_ids=(record.record_id,),
            calculation=(
                f"Visit record {record.record_id} ({record.visit_label}, "
                f"{record.visit_date.describe()}, {record.site_id}) has subject_id "
                f"{record.raw_subject_id!r}. There is no defensible way to determine whose "
                f"visit this is, and attributing it by site or proximity would put a "
                f"deviation on the wrong participant's record."
            ),
            proposed_actions=(ProposedAction.ESCALATE_TO_MEDICAL_MONITOR,
                              ProposedAction.RAISE_SITE_QUERY),
            evidence=(f"Site comment: {record.comment!r}",) if record.comment else (),
        ))

    for record in study.unknown_subject_visits:
        findings.append(Finding(
            detector="unattributable_record",
            family=Family.DATA_QUALITY,
            verdict=Verdict.NOT_ASSESSABLE,
            subject_id=record.subject_id, site_id=record.site_id,
            visit_id=record.visit_id, record_ids=(record.record_id,),
            calculation=(
                f"Visit record {record.record_id} references subject "
                f"{record.raw_subject_id!r}, which does not appear in subjects.json. "
                f"Either the subject is missing from the enrolment file or the identifier "
                f"is wrong; both are questions for the site."
            ),
            proposed_actions=(ProposedAction.RAISE_SITE_QUERY,),
        ))

    for record in study.unknown_subject_doses:
        findings.append(Finding(
            detector="unattributable_record",
            family=Family.DATA_QUALITY,
            verdict=Verdict.NOT_ASSESSABLE,
            subject_id=record.subject_id, visit_id=record.visit_id,
            record_ids=(record.record_id,),
            calculation=(
                f"Dose record {record.record_id} references subject "
                f"{record.raw_subject_id!r}, which does not appear in subjects.json."
            ),
            proposed_actions=(ProposedAction.RAISE_SITE_QUERY,),
        ))

    return findings


# --------------------------------------------------------------------------
# Site-level data quality -- a separate family, never the deviation log
# --------------------------------------------------------------------------

def site_data_quality(study: Study, findings: list[Finding]) -> list[Finding]:
    """Patterns about the site's records rather than about the protocol.

    "SITE-03 accounts for 68% of unassessable records" is a data quality
    finding. Routing it to the deviation log would inflate the deviation rate
    the sponsor reports.
    """
    results: list[Finding] = []

    unassessable = [f for f in findings if f.verdict is Verdict.NOT_ASSESSABLE]
    if unassessable:
        by_site: dict[str, int] = defaultdict(int)
        for finding in unassessable:
            by_site[finding.site_id or "(unattributed)"] += 1
        worst_site, worst_count = max(sorted(by_site.items()), key=lambda kv: kv[1])
        share = Decimal(worst_count) / Decimal(len(unassessable)) * 100
        if share >= 50:
            results.append(Finding(
                detector="site_data_quality",
                family=Family.DATA_QUALITY,
                verdict=Verdict.NOT_ASSESSABLE,
                site_id=worst_site,
                calculation=(
                    f"{worst_site} accounts for {worst_count} of {len(unassessable)} "
                    f"unassessable records ({share:.0f}%). Breakdown: "
                    + ", ".join(f"{site} {count}" for site, count in sorted(by_site.items()))
                    + ". This is a data quality finding about the site, not a protocol "
                      "deviation against any participant, and it must not be filed in the "
                      "deviation log."
                ),
                proposed_actions=(ProposedAction.OPEN_CAPA,),
            ))

    normalised = study.normalised_id_count
    if normalised:
        sites = sorted({r.site_id for r in study.visits if r.id_was_normalised if r.site_id})
        results.append(Finding(
            detector="site_data_quality",
            family=Family.DATA_QUALITY,
            verdict=Verdict.COMPLIANT,
            site_id=sites[0] if len(sites) == 1 else None,
            calculation=(
                f"{normalised} record(s) could be matched to a subject only after "
                f"normalising the identifier format"
                + (f", all at {sites[0]}" if len(sites) == 1 else "")
                + ". The join succeeded, so no assessment was lost, but a site writing the "
                  "same participant several ways is a monitoring signal in its own right."
            ),
            proposed_actions=(ProposedAction.OPEN_CAPA,),
        ))

    lags = [(r.site_id, r.entry_lag_days) for r in study.visits
            if r.entry_lag_days is not None and r.site_id]
    by_site_lag: dict[str, list[int]] = defaultdict(list)
    for site_id, lag in lags:
        by_site_lag[site_id].append(lag)
    for site_id in sorted(by_site_lag):
        values = by_site_lag[site_id]
        late = [v for v in values if v > LATE_ENTRY_DAYS]
        if len(late) < len(values) / 2 or not late:
            continue
        results.append(Finding(
            detector="site_data_quality",
            family=Family.DATA_QUALITY,
            verdict=Verdict.COMPLIANT,
            site_id=site_id,
            calculation=(
                f"{site_id} entered {len(late)} of {len(values)} visit records more than "
                f"{LATE_ENTRY_DAYS} days after the visit (median "
                f"{sorted(values)[len(values) // 2]} days, maximum {max(values)}). "
                f"Late entry lowers the reliability of the data and is a monitoring signal, "
                f"but it is not a protocol deviation and must not be filed as one."
            ),
            proposed_actions=(ProposedAction.OPEN_CAPA,),
        ))

    return results


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------

DETECTORS = (
    missed_visit,
    out_of_window_visit,
    missing_assessment,
    dose_deviation,
    consent_sequence,
    eligibility_breach,
    unattributable_record,
)


def run_all(study: Study) -> list[Finding]:
    """Every detector, then the aggregating ones that depend on their output."""
    findings: list[Finding] = []
    for detector in DETECTORS:
        findings.extend(detector(study))

    findings = assign_ids(findings)
    patterns, findings = systemic_pattern(study, findings)
    quality = site_data_quality(study, findings)

    numbered = assign_ids(patterns + quality, start=len(findings) + 1)
    return findings + numbered
