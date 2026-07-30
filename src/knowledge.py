"""Key concepts, answered from a registry rather than retrieved.

No vector store and no RAG. The concept set is small, closed and known at build
time; embedding it would add an approximate lookup to a question with an exact
answer, and an approximate miss here fabricates a regulatory claim.
"""

from __future__ import annotations

KEY_CONCEPTS: dict[str, str] = {
    "protocol deviation": (
        "Any departure from the approved protocol. This is the term ICH E6(R3) uses; "
        "'protocol violation' does not appear in the guideline and is best avoided."
    ),
    "important deviation": (
        "The regulated subset: a deviation that might significantly affect the "
        "completeness, accuracy or reliability of the study data, or the participant's "
        "rights, safety or well-being. Classification is the investigator's decision, "
        "not the system's -- this agent only proposes one, with its reasoning."
    ),
    "immediate hazard": (
        "ICH E6(R3) Section 2.5.3 permits deviating from the protocol where necessary to "
        "eliminate an immediate hazard to a trial participant. It is still a deviation "
        "and is still documented -- but it does not warrant a corrective action against "
        "a site that did the right thing."
    ),
    "capa": (
        "Corrective and preventive action. For important deviations E6(R3) requires "
        "measures to prevent recurrence, not merely documentation. Documentation alone "
        "does not close the loop, and that is what inspectors check."
    ),
    "visit window": (
        "The permitted range around a protocol-specified visit day, counted from the "
        "subject's own Day 1. Windows differ per visit -- Day 1 is exact here, End of "
        "Treatment allows +/-14 days -- so a single global tolerance gives wrong answers "
        "at both ends."
    ),
    "governing protocol version": (
        "The version a subject consented under, which governs them for the whole study. "
        "Not the version in force on the calendar date of a visit. Measuring a subject "
        "against the wrong version fabricates deviations or suppresses real ones."
    ),
    "not assessable": (
        "The third verdict. The source data is incomplete or ambiguous, so the record "
        "cannot be judged against the protocol at all. It routes to a site query, never "
        "to a deviation record. A system offering only 'deviation' and 'compliant' turns "
        "missing data into false negatives."
    ),
    "site query": (
        "A request to the site to supply or correct data. The correct destination for an "
        "unassessable record, and for a data entry problem that is not a protocol "
        "deviation."
    ),
    "screen failure": (
        "A participant who consented and was screened but did not meet eligibility and "
        "was never enrolled. Protocol compliance does not apply to them; a deviation "
        "filed against a screen failure is a false positive."
    ),
    "protocol amendment": (
        "A change to the protocol itself. The right remediation when most subjects at a "
        "site miss the same window: the window is too tight, and filing N deviation "
        "reports is both noise and the wrong fix."
    ),
    "anchor date": (
        "Day 1, the subject's first dose. Every visit window is computed from it, and it "
        "is a different calendar date for every subject. Computing from the enrolment or "
        "consent date instead silently shifts every window in the study."
    ),
    "house terminology": (
        "'Major', 'minor' and 'critical' are sponsor and CRO house terms, not regulatory "
        "categories. E6(R3) distinguishes only deviations and important deviations."
    ),
}

ALIASES = {
    "violation": "protocol deviation",
    "deviation": "protocol deviation",
    "important": "important deviation",
    "2.5.3": "immediate hazard",
    "hazard": "immediate hazard",
    "corrective action": "capa",
    "preventive action": "capa",
    "window": "visit window",
    "version": "governing protocol version",
    "unassessable": "not assessable",
    "query": "site query",
    "amendment": "protocol amendment",
    "day 1": "anchor date",
    "anchor": "anchor date",
    "major": "house terminology",
    "minor": "house terminology",
    "critical": "house terminology",
}


def answer_key_concept(message: str) -> str:
    text = (message or "").lower()
    matched: list[str] = []

    for key in KEY_CONCEPTS:
        if key in text:
            matched.append(key)
    for alias, key in ALIASES.items():
        if alias in text and key not in matched:
            matched.append(key)

    if not matched:
        return ("The key concepts registry covers: "
                + ", ".join(sorted(KEY_CONCEPTS)) + ".")
    return "\n\n".join(f"**{key.title()}** — {KEY_CONCEPTS[key]}" for key in matched[:3])
