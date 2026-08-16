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
from decimal import Decimal

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
from src.thresholds import DEFAULTS, rationale_for
from src.verdicts import Verdict

# Every number that turns evidence into a verdict lives in src/thresholds.py,
# with the reasoning behind it and what happens if it moves. A finding that
# depends on one names it.
T = DEFAULTS
DOSE_TOLERANCE = T.dose_tolerance
DOSE_IMPORTANT_THRESHOLD = T.dose_important
SYSTEMIC_MIN_SUBJECTS = T.systemic_min_subjects
SYSTEMIC_MIN_SHARE = T.systemic_min_share
LATE_ENTRY_DAYS = T.late_entry_days


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
                        f"{scheduled.label}: no visit record; dose record "
                        f"{doses[0].record_id} administered "
                        f"{doses[0].dose_date.describe()}. Window "
                        f"{scheduled.opens.isoformat()} to {scheduled.closes.isoformat()}."
                    ),
                    rationale=(
                        "A dose record proves the visit happened, so this is a missing "
                        "record rather than a missed visit. Whether it fell inside the "
                        "window cannot be assessed without the visit record."
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
                    f"{scheduled.describe()}. No visit record; window closed "
                    f"{scheduled.closes.isoformat()}, "
                    f"{(study.as_of - scheduled.closes).days} days ago."
                    + (" Required assessments included a dose." if requires_dose else "")
                ),
                rationale=(
                    "Absence of a record is only a finding once the window has shut; "
                    "before that the visit is simply not due yet."
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
                    rationale=(
                        "The recorded date does not place the visit inside or outside "
                        "the window. Imputing a day and then issuing a verdict would "
                        "fabricate a finding."
                    ),
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
                days_out=days,
                calculation=(
                    f"{assessment.calculation} (record {record.record_id}) "
                    f"Governing protocol version: {resolved.version.label} "
                    f"(consented {subject.consent_date.describe()})."
                ),
                rationale=resolved.explanation,
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
                # The deviation is real, and it is already on the record.
                # Filing it again double-reports.
                finding = replace(
                    finding,
                    suppressed_by=f"deviation_log:{already.deviation_id}",
                    proposed_actions=(),
                    calculation=(
                        f"{finding.calculation} Already recorded as "
                        f"{already.deviation_id}, classified "
                        f"{already.classification!r}."
                    ),
                    rationale=(
                        "The deviation is genuine but is already on the record. Filing "
                        "it again double-reports and inflates the deviation rate the "
                        "sponsor reports."
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
            f"{len(records)} records for {scheduled.label}: {dates}. Neither is voided."
        ),
        rationale=(
            "Which date is authoritative is a question for the site. The visit must "
            "not be counted twice and a date must not be picked silently."
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
                    f"{scheduled.label}: {version.label} requires "
                    f"{', '.join(scheduled.required_assessments)}. Record "
                    f"{record.record_id} ({record.visit_date.describe()}) records "
                    f"{', '.join(record.assessments_done) or 'nothing'}. "
                    f"Missing: {', '.join(missing)}."
                ),
                rationale=(
                    f"{subject_id} consented under {version.label}, which is what makes "
                    f"{', '.join(missing)} required here. The identical record for a "
                    f"subject governed by an earlier version would be compliant."
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
                        f"Dose record {record.record_id} dated "
                        f"{record.dose_date.describe()}."
                    ),
                    rationale=(
                        "Without a precise date the weight as of that day cannot be "
                        "resolved, so the expected dose cannot be computed."
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
                        f"Dose record {record.record_id}: {record.raw_dose!r} "
                        f"{record.dose_unit!r} on {when.isoformat()}. Expected dose "
                        f"not computable."
                    ),
                    rationale=expected.explanation,
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
                        f"Expected {expected.dose} mg on {when.isoformat()}; dose "
                        f"record {record.record_id} cannot be read ({problem})."
                    ),
                    rationale=expected.explanation,
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
                    f"Expected {expected.dose} mg (5 mg/kg x {expected.weight_used} kg "
                    f"as of {when.isoformat()}, record {expected.weight_record_id}); "
                    f"administered {given} mg (record {record.record_id}). Difference "
                    f"{abs(difference)} mg, {percent}% {direction} expected. Tolerance "
                    f"+/-{DOSE_TOLERANCE * 100:.0f}%."
                ),
                rationale=(expected.explanation + " "
                           + rationale_for("dose_tolerance") + " "
                           + rationale_for("dose_important")),
                threshold_applied=("dose_important" if important else "dose_tolerance"),
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
            f"Dose record {record.record_id} on {record.dose_date.describe()}: "
            f"dose_status 'Withheld', no administered amount. The protocol schedules a "
            f"dose at this visit and permits holding only for: {criteria}. A withheld "
            f"dose not meeting a stated hold criterion departs from the protocol. "
            f"ICH E6(R3) Section 2.5.3 permits a deviation made to eliminate an "
            f"immediate hazard to the participant; where it applies the deviation is "
            f"documented but does NOT warrant a corrective action against the site."
        ),
        rationale=(
            "A clinical rationale is recorded on this record and is quoted as evidence "
            "for the investigator and medical monitor to confirm. It is deliberately "
            "not parsed to reach this verdict: whether 2.5.3 applies is a clinical "
            "judgement, and the coded dose_status is what drives the routing here."
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
                        f"Consent {consent.describe()}; {record.visit_label} "
                        f"(record {record.record_id}) dated "
                        f"{record.visit_date.describe()}, "
                        f"{gap if gap is not None else f'between {-high} and {-low}'} "
                        f"day(s) before consent. Assessments performed: "
                        f"{', '.join(record.assessments_done) or 'none'}."
                    ),
                    rationale=(
                        "A study procedure performed before informed consent is not a "
                        "matter of degree: one day before consent is still before "
                        "consent."
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
                        f"Consent {consent.describe()}; {record.visit_label} "
                        f"(record {record.record_id}) dated "
                        f"{record.visit_date.describe()} -- between {-low} day(s) before "
                        f"and {high} day(s) after consent."
                    ),
                    rationale=(
                        "Whether the procedure preceded consent cannot be answered from "
                        "these dates."
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
            ),
            rationale=(
                "A subject who failed eligibility must not be enrolled or dosed. The "
                "inverse is not a finding: a screen failure who was never enrolled is "
                "simply a screen failure."
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
                f"({share * 100:.0f}%) fell outside the {label} window. "
                + "; ".join(
                    f"{f.subject_id} "
                    + (f"{abs(f.days_out)} day(s) "
                       f"{'late' if f.days_out > 0 else 'early'}"
                       if f.days_out is not None else "outside the window")
                    + (f" ({f.record_ids[0]})" if f.record_ids else "")
                    for f in sorted(group, key=lambda x: x.subject_id or "")
                )
                + "."
            ),
            rationale=(
                f"A window missed by most of a site's subjects indicates the window is "
                f"too tight for the site's operating conditions, not that {len(group)} "
                f"separate errors occurred. Filing {len(group)} deviation reports would "
                f"be the wrong remediation and would hide the real finding. "
                + rationale_for("systemic_min_subjects")
            ),
            threshold_applied="systemic_min_subjects",
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
                f"{record.raw_subject_id!r}."
            ),
            rationale=(
                "There is no defensible way to determine whose visit this is. "
                "Attributing it by site or proximity would put a deviation on the "
                "wrong participant's record."
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
                f"{record.raw_subject_id!r}, absent from subjects.json."
            ),
            rationale=(
                "Either the subject is missing from the enrolment file or the "
                "identifier is wrong; both are questions for the site."
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
                verdict=None,
                site_id=worst_site,
                calculation=(
                    f"{worst_site} accounts for {worst_count} of {len(unassessable)} "
                    f"unassessable records ({share:.0f}%). Breakdown: "
                    + ", ".join(f"{site} {count}" for site, count in sorted(by_site.items()))
                    + "."
                ),
                rationale=(
                    "A data quality finding about the site, not a protocol deviation "
                    "against any participant. It must not be filed in the deviation "
                    "log: doing so would inflate the deviation rate the sponsor "
                    "reports to the regulator."
                ),
                proposed_actions=(ProposedAction.OPEN_CAPA,),
            ))

    normalised = study.normalised_id_count
    if normalised:
        sites = sorted({r.site_id for r in study.visits if r.id_was_normalised if r.site_id})
        results.append(Finding(
            detector="site_data_quality",
            family=Family.DATA_QUALITY,
            verdict=None,
            site_id=sites[0] if len(sites) == 1 else None,
            calculation=(
                f"{normalised} record(s) matched to a subject only after normalising "
                f"the identifier format"
                + (f", all at {sites[0]}" if len(sites) == 1 else "") + "."
            ),
            rationale=(
                "The join succeeded, so no assessment was lost. A site writing the "
                "same participant several ways is a monitoring signal in its own "
                "right, and the count of records that matched only after "
                "normalisation is the finding."
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
            verdict=None,
            site_id=site_id,
            calculation=(
                f"{site_id} entered {len(late)} of {len(values)} visit records more "
                f"than {LATE_ENTRY_DAYS} days after the visit (median "
                f"{sorted(values)[len(values) // 2]} days, maximum {max(values)})."
            ),
            rationale=(
                "Late entry lowers the reliability of the data and is a monitoring "
                "signal, but it is not a protocol deviation and must not be filed as "
                "one. " + rationale_for("late_entry_days")
            ),
            threshold_applied="late_entry_days",
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


def consolidate_site_queries(findings: list[Finding]) -> list[Finding]:
    """One query per record, not one per detector that noticed it.

    A partial date on a visit is noticed by the timing detector, the dose
    detector and the consent detector, and each would raise its own query about
    the same record. A site receiving three separate questions about one row is
    a system failing at the human end of the loop, however correct each question
    is on its own.

    The findings are not merged -- each keeps its own verdict and reasoning. Only
    the query action is consolidated onto the first finding for that record, so
    exactly one goes out.
    """
    seen: dict[str, str] = {}
    result: list[Finding] = []
    for finding in findings:
        actions = list(finding.proposed_actions)
        if ProposedAction.RAISE_SITE_QUERY in actions:
            # The unit is the visit occasion, not the row. A visit record and
            # its dose record are different rows describing one event, and a
            # site expects one question about that event.
            if finding.subject_id and finding.visit_id:
                key = f"{finding.subject_id}/{finding.visit_id}"
            elif finding.record_ids:
                key = "|".join(sorted(finding.record_ids))
            else:
                result.append(finding)
                continue
            owner = seen.setdefault(key, finding.finding_id)
            if owner != finding.finding_id:
                actions.remove(ProposedAction.RAISE_SITE_QUERY)
                finding = replace(
                    finding, proposed_actions=tuple(actions),
                    rationale=(finding.rationale + " The site query for "
                               f"{key} is raised once, on {owner}.").strip(),
                )
                result.append(finding)
                continue
        result.append(finding)
    return result


def run_all(study: Study) -> list[Finding]:
    """Every detector, then the aggregating ones that depend on their output."""
    findings: list[Finding] = []
    for detector in DETECTORS:
        findings.extend(detector(study))

    findings = assign_ids(findings)
    patterns, findings = systemic_pattern(study, findings)
    findings = consolidate_site_queries(findings)
    quality = site_data_quality(study, findings)

    numbered = assign_ids(patterns + quality, start=len(findings) + 1)
    return findings + numbered
