"""Three verdicts, not two.

The single most important idea in this design. A system that can only say
`deviation` or `compliant` silently converts missing data into false negatives:
asked whether a visit dated "2025-06" fell inside a +/-3 day window, it has to
answer one or the other, and both answers are inventions.

`not_assessable` is not a failure state. It is the honest answer, and it routes
to a site query rather than to a deviation record.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Iterable


class Verdict(StrEnum):
    DEVIATION = "deviation"
    COMPLIANT = "compliant"
    NOT_ASSESSABLE = "not_assessable"

    @property
    def is_finding(self) -> bool:
        """A deviation is filed. An unassessable record is queried. A compliant
        one is neither."""
        return self is Verdict.DEVIATION


def combine(verdicts: Iterable[Verdict]) -> Verdict:
    """Roll several verdicts about the same subject up into one.

    Not-assessable dominates: if any part of the question cannot be answered,
    the answer to the whole question is not "compliant". Reporting compliance
    over data you could not read is the failure mode this whole module exists
    to prevent.
    """
    seen = list(verdicts)
    if not seen:
        return Verdict.NOT_ASSESSABLE
    if any(v is Verdict.NOT_ASSESSABLE for v in seen):
        return Verdict.NOT_ASSESSABLE
    if any(v is Verdict.DEVIATION for v in seen):
        return Verdict.DEVIATION
    return Verdict.COMPLIANT
