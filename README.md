# Protocol Deviation Agent

> I am not a clinical research professional. The domain model here is derived from
> the public ICH E6(R3) guideline and from protocol and statistical analysis plan
> documents published on clinicaltrials.gov. All data is synthetic. Classification
> thresholds are illustrative and would require validation by a qualified clinical
> team before any real use. What is demonstrated is the oversight architecture:
> deterministic detection, surfaced ambiguity, and a human gate on every write.

A chat agent that reviews clinical trial data against a protocol, proposes
corrective actions, and writes nothing without a named human approving it.

The interesting claim is not that it finds deviations. It is that it **knows what
it cannot assess** — and says so, instead of turning missing data into a
confident answer.

## Run

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows;  source .venv/bin/activate elsewhere
pip install -r requirements.txt

python scripts/generate_data.py   # deterministic synthetic study
python reset_sandbox.py
pytest -q

streamlit run app.py              # chat UI
python demo.py                    # offline CLI walkthrough
python scripts/diff_findings.py   # detector output vs. planted ground truth
```

No API key is needed. Without `ANTHROPIC_API_KEY` the agent uses a deterministic
rule-based intent classifier; with one it uses Claude Haiku 4.5. Either way the
LLM classifies *intent only* — every figure the user sees comes from a tool
result.

## Three verdicts, not two

The single most important design decision.

| Verdict | Meaning | Routes to |
|---|---|---|
| `deviation` | Assessed against the protocol, and it departs from it | Deviation log |
| `compliant` | Assessed against the protocol, and it conforms | Nothing |
| `not_assessable` | **Cannot** be assessed — the source data is incomplete or ambiguous | Site query |

A system that outputs only the first two silently converts missing data into
false negatives. Asked whether a visit dated `"2025-06"` fell inside a ±3 day
window, it must answer `deviation` or `compliant`, and both answers are
inventions.

In this dataset 11% of assessable units cannot be assessed, and one site accounts
for 68% of them. Reporting forty deviations from that data would not be thorough;
it would be confidently wrong in a regulated setting.

`not_assessable` is not the same as "give up". A visit recorded as `"2025-10"`
against an August window is still unambiguously late — every day in the interval
falls outside — so it is a `deviation`. A partial date is modelled as an
*interval* and an ambiguous date as a *set of candidate days*; the verdict falls
out of the arithmetic. Refusing to answer where the answer is determined would be
a false negative dressed up as caution.

## Graph

```text
START → classify
  ├─ review              → run every detector, draft actions   → END
  ├─ follow_up           → answer from state                   → END
  ├─ knowledge_question  → key concepts registry               → END
  ├─ audit_query         → exact structured filter             → END
  ├─ unknown                                                    → END
  └─ gate ──confirmed?──→ apply → END
            └─refused───→ END
```

**Two tool tiers.** Reading and drafting run freely — `review` loads the study,
runs eight detectors and drafts corrective actions, and none of it persists
anything outside the proposals ledger. Writing is gated.

**`apply` is the only node that mutates a ledger, and `gate` is its only
predecessor.** [tests/test_gate.py](tests/test_gate.py) reads the compiled graph
and walks every path from `START` to prove it. A comment claiming that invariant
would be worth nothing.

**The gate really pauses.** It calls LangGraph's `interrupt()` with the approval
card and waits for a human turn. It does not pre-decide and it does not proceed
on silence.

### State

| Field | Why it exists |
|---|---|
| `active_subject_id` / `active_site_id` | Makes *"why was that one a deviation?"* work without repeating the ID. A newly named subject **replaces** it rather than merging — falling back to the previous subject when the user names a site answers a question nobody asked. |
| `findings` | What follow-ups are answered from, so the model never recomputes a figure. |
| `pending_actions` | What the gate approves *against*. An approval refers to a stored draft, never to what the model remembers. |
| `applied_actions` | What makes rollback possible. |
| `gate_action_id` / `gate_decision` / `gate_passed` / `gate_verbatim` | Set by the gate, read by the single write node. The verbatim text travels into the audit trail unparaphrased. |

## Graduated confirmation

A routine site query takes a plain affirmative. Two things require the exact
token `APPROVE ACT-XXXXXXXX`:

- classifying something as an **important deviation** — it reaches the IRB/EC;
- **escalating to the medical monitor** — it starts a clock.

That is not ceremony. A plain "yes" on either writes nothing and explains why.

## The five actions

| Action | When |
|---|---|
| `log_deviation` | The default record, with a *proposed* classification |
| `raise_site_query` | Data is missing or needs correction |
| `open_capa` | Systemic or recurring — the prevention step E6(R3) requires |
| `propose_protocol_amendment` | The protocol is the problem, not the site |
| `escalate_to_medical_monitor` | Safety-relevant, time-bound |

Every classification is a **proposal carrying its reasoning**, and says so in its
own data. E6(R3) places the review of deviations on the investigator — a named,
accountable human. A system that writes classifications unattended is not merely
risky; it is misaligned with the guideline it claims to serve.

`major` / `minor` / `critical` are sponsor house terminology and are deliberately
not used.

## Retrieval, and why there is none

No vector store, no embeddings, no RAG. Every lookup is an exact match on a
normalised key. The argument is stronger here than in billing: an approximate
retrieval miss does not merely lose a row, it **fabricates a safety finding or
hides one**. The key concepts registry is small, closed and known at build time;
embedding it would add an approximate lookup to a question with an exact answer.

Normalisation happens once, on load, and every normalisation is *recorded* rather
than performed silently — the count of records that matched only after
normalising the subject ID is itself a finding about the site.

## Two finding families, kept apart

**Protocol deviations** are about the subject and the protocol; they go to the
deviation log and, depending on classification, to the IRB/EC. **Data quality
findings** are about the site's records; they go to a query or a CAPA.

Conflating them is a real-world mistake. Filing a data entry problem as a
deviation inflates the deviation rate the sponsor reports to the regulator. The
detectors will not propose `log_deviation` for a data quality finding, and a test
enforces it.

## Decisions and tradeoffs

**A weight that exists on the visit date but is unusable blocks the dose, rather
than falling back to an earlier one.** The as-of rule ("most recent measurement
on or before the date") would happily reach back to the previous visit and return
a confident number. *Given up:* fewer answers. *Bought:* an earlier visit's
weight is not this visit's weight, and substituting it is an undeclared
imputation that also hides the data quality problem. The fallback still applies
when no measurement exists on the date at all.

**Compliance is the absence of a finding.** *Given up:* the output cannot be read
as a complete per-visit ledger. *Bought:* the four findings that matter are not
buried under ninety that do not.

**No verdict is ever parsed out of free text.** The withheld dose carries its
clinical rationale in a prose `reason` field. The *coded* `dose_status` drives
the routing; the note is quoted for the investigator. *Given up:* the system
cannot conclude that §2.5.3 applies. *Bought:* it cannot wrongly conclude that
either, and §2.5.3 applicability is a clinical judgement.

**Six illustrative thresholds drive every verdict** — dose tolerance 10%,
important at 20%, systemic pattern at 3 subjects and 50% of a site, late entry at
14 days, plausible weight 30–250 kg. They are stated in
[src/detectors.py](src/detectors.py) and in the finding text so a reader can see
exactly what drove an answer. A qualified clinical team would set them.

**15 subjects, not 12.** Twelve cannot carry twelve traps, a four-subject
systemic pattern and a four-subject clean control group. A dataset where
everything is a finding proves nothing.

**A JSON sandbox, not a clinical database.** Authentication, multi-user
authorisation, durable transactions, EDC integration and an electronic signature
compliant with 21 CFR Part 11 are all out of scope. The approval gate here
records who approved and what they typed; it does not authenticate them.

## Layout

```
scripts/generate_data.py   deterministic synthetic study + ground truth
scripts/diff_findings.py   detector output vs. ground truth (the Phase 4 gate)
src/quantities.py          unit-aware Decimal; rounds once, at the boundary
src/dosing.py              dose normalisation with as-of weight + provenance
src/dates.py               partial dates as intervals, ambiguous as candidate sets
src/verdicts.py            the three verdicts
src/protocol.py            version lineage, per-subject windows
src/study.py               loading and exact indexing
src/detectors.py           the eight detectors + two aggregating
src/findings.py            findings and classification proposals
src/actions.py             drafts, graduated confirmation, the executor
src/sandbox.py             five ledgers, version counter, atomic writes
src/audit_trail.py         hash-chained append-only trail
src/graph/                 the graph, the gate, the single write node
```

See [docs/DATA_TRAPS.md](docs/DATA_TRAPS.md) for the planted ground truth and
[docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) for the walkthrough.
