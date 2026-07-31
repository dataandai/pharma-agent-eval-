# Architecture

The README covers the graph shape and the state fields. This is the part that is
easy to lose sight of once the code grows: who is allowed to decide what, and the
invariants that hold no matter which path a conversation takes.

## Responsibility split

| Concern | Owner |
|---|---|
| Intent, and resolving "that subject" in a follow-up | LLM boundary (or the offline rule-based classifier) |
| Subject and record matching | Deterministic Python, exact keys only |
| Dates, windows, unit conversion, dose arithmetic | Deterministic Python, `Decimal` |
| Which protocol version governs a subject | Deterministic Python, from the consent record |
| Whether a record can be assessed at all | Deterministic Python |
| Proposing a classification, with reasoning | Deterministic Python |
| **Deciding** a classification | The investigator, in chat |
| Sandbox writes and rollback | Deterministic executor, behind the gate |
| Explanation shown to the user | Structured evidence rendered to text |

The line that matters: the model chooses **which question is being asked**, never
**what the answer is**. Every figure in a response came from a tool result.

## Invariants

1. **The model never computes.** No arithmetic, no unit conversion, no date
   comparison in the LLM.
2. **`Decimal` throughout, rounded once at the boundary.** Intermediate rounding
   turns a correct dose into an apparent deviation.
3. **Exact indexing only.** No vector store, no embeddings. An approximate
   retrieval miss fabricates a safety finding or hides one.
4. **Normalisation is recorded, never silent.** The count of records that matched
   only after normalising an identifier is itself a finding about the site.
5. **Three verdicts.** `not_assessable` is a first-class answer and routes to a
   site query, never to the deviation log.
6. **A verdict is a claim about one record's protocol compliance.** Site-level
   observations make no such claim and carry none.
7. **Classification is proposed, never decided.** Every proposal carries its
   reasoning and says in its own data that it is a proposal.
8. **Two finding families stay apart.** A data quality problem never proposes a
   deviation record.
9. **`apply` is the only node that writes, and `gate` is its only predecessor.**
   Enforced by a test that walks the compiled graph, not by convention.
10. **The gate pauses.** It does not pre-decide and does not proceed on silence.
11. **Graduated confirmation.** An important-deviation classification or a
    medical monitor escalation needs the exact token; nothing else does.
12. **Approval is recorded verbatim.** Paraphrasing a human's approval into a
    boolean is how accountability is lost.
13. **A stale draft is refused.** If the sandbox changed after the human was
    shown the card, the approval no longer refers to the world they reviewed.
14. **Execution and rollback are idempotent.**
15. **Rollback compensates, never deletes.** The original record stays, marked
    reversed.
16. **The audit trail is append-only and hash-chained**, and the chain is
    verifiable.
17. **JSON writes are atomic.**
18. **Free text is never parsed to reach a verdict.** Coded fields decide
    routing; prose is quoted as evidence for a human.

## Where each invariant is tested

| Invariant | Test |
|---|---|
| 2 | `tests/test_quantities.py::test_conversion_does_not_round` |
| 3, 4 | `tests/test_dosing.py`, `tests/test_protocol.py` |
| 5, 6 | `tests/test_dates.py`, `tests/test_detectors.py` |
| 7, 8 | `tests/test_detectors.py` |
| 9 | `tests/test_gate.py::test_no_other_node_can_reach_the_write_node` |
| 10, 11 | `tests/test_gate.py` |
| 12, 16 | `tests/test_gate.py::test_the_audit_entry_records_everything_an_inspector_needs` |
| 13, 14, 15 | `tests/test_gate.py` |
| 18 | `tests/test_detectors.py::test_the_hazard_verdict_is_not_parsed_out_of_the_free_text` |

## Data flow

```text
data/*.json
   │  exact indexing, normalisation recorded
   ▼
Study ──► detectors ──► Findings (verdict + calculation + proposed classification)
                            │
                            │  reportable only; suppressed ones draft nothing
                            ▼
                        ActionDrafts ──► proposals ledger (not a write to any
                            │                              corrective-action ledger)
                            ▼
                          gate  ◄──── human, in chat, verbatim
                            │
                            ▼
                          apply ──► one of five ledgers + audit entry
```

Nothing crosses the `gate` line without a human turn, and nothing below it runs
if the confirmation is not accepted.
