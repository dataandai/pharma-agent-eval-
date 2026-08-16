# Protocol Deviation Agent

The Protocol Deviation Agent helps clinical teams find and document departures
from a trial's protocol faster and with less manual work. It runs precise,
deterministic checks on study data to surface potential issues, explains what
happened and why, and prepares a clear recommended action for a human reviewer.
We use lightweight "agents" to coordinate the rule-based detectors and the
conversational interface: the agent routes requests, runs the checks, and
packages verified results, while a language model only helps determine what the
user is asking. Crucially, nothing is written to official records until a named
human reviews and explicitly approves — preserving auditability and
accountability. You can interact via the web UI (`streamlit run app.py`) or an
operational worker (for example, an AWS SQS-backed agent).

---

## What this is, in plain language

When a new drug is tested on people, the trial has a very detailed rulebook. It
is called the **protocol**. It says which patient must come back for which check,
what has to be measured at that visit, and what dose they should receive.

Reality is never that tidy. A patient turns up three days late. A lab result
never makes it into the system. One site records body weight in pounds while
another uses kilograms. Someone mistypes a date.

Finding and documenting those departures is **mandatory**. If a regulator
inspects later and discovers an unrecorded one, it can call the whole trial's
result into question — years of work and the participation of thousands of
people.

The problem is that today this is largely manual. People sit over spreadsheets
checking whether patient 47's twelfth visit fell inside the permitted window.
Hundreds of patients, thousands of visits.

This program reads the patient data and the protocol, compares them, explains
every departure it finds, and **proposes** what to do about it. It writes nothing
anywhere until a named, responsible person approves.

### Four examples, all from the built-in sample data

**1. Two patients, the same delay, opposite answers.** Subjects S-004 and S-009
were both **exactly four days late** for the same check-up. For one it is a
protocol departure; for the other it is not. The rulebook was amended
mid-trial — the permitted slippage was widened from three days to five — and
whoever consented under the old rules **stays under the old rules**, even if
their visit happened after the change took effect. This error slips in very
easily by hand, and it is wrong in both directions: it either hides a real
departure or invents one that never happened.

**2. Four patients were late — and it is not their fault.** At one site, four of
five patients missed the same visit window by four or five days. The obvious
reaction would be four incident reports. That is the **wrong answer**. If 80% of
a site's patients cannot make the window, four separate mistakes did not happen —
the window is too tight. So the program proposes one thing: amend the protocol.
It keeps the four individual findings on record but does not file them
separately. Four reports would not merely be noise; they would bury the real
finding.

**3. The doctor did the right thing — and must not be penalised for it.** For
subject S-005 the investigator withheld a scheduled dose because the patient's
blood counts had deteriorated. That is **formally a departure** — the protocol
prescribes a dose that day — so it must be documented. But the international
guideline (ICH E6(R3) §2.5.3) explicitly permits departing from the rulebook to
protect a participant from immediate harm. So: record it, **but do not open a
corrective action against the site.** A naive system would automatically
discipline a doctor who did exactly what they should have. This one knows the
difference.

**4. "I cannot tell you that."** Possibly the most important one. Some records
simply **cannot** yield an answer. If a visit date reads only "June 2025", you
cannot decide whether it fell inside a three-day window. If a weight has no unit
next to it, you cannot compute the correct dose. Most systems guess, or say
"fine". Both are lies. This program gives a third answer: *"I cannot assess this,
and here is the question I would put to the site."* In the sample data that
covers 11% of records, two thirds of them from a single site — which is itself
worth knowing.

### Why this is worth building

Three things separate it from a typical "AI-powered" solution.

**The language model does no arithmetic.** The AI has exactly one job:
understanding what the user is asking for. Every figure, date and comparison
comes from ordinary, checkable program code. If it says "408 mg", that *is*
408 mg — not a number that is probably right.

**It writes nothing without a human.** Not for the sake of strictness. A
departure record goes into the trial's official file and, for the more serious
ones, to an ethics committee. The guideline places that decision with the
**investigator** — a named, accountable person. So the program only ever
proposes, and always states why. For the weightier decisions — classifying
something as an *important deviation*, or escalating to a medical monitor — an
"ok" is not enough; an exact confirmation phrase is required, because those
carry regulatory consequences.

**It tells you what it does not know.** A system reporting forty departures from
data of which a quarter is unreadable is not thorough. It is confidently wrong,
in a strictly regulated field.

### Who it helps

- **Clinical monitors**, who oversee the sites: instead of manual spreadsheet
  work they get a list where every claim carries its calculation and the ID of
  the record it came from. They do not have to trust it — they can look it up.
- **Investigators**: the classification decision stays theirs, but arrives with a
  prepared rationale and all the relevant data in one place.
- **Data managers**: data quality problems and genuine protocol departures are
  kept apart. That matters — filing a mistyped date as a departure needlessly
  inflates the deviation rate the sponsor reports to the regulator.
- **Patients**, indirectly: the less attention goes to noise, the more is left
  for real safety signals.

### What it does not do

> I am not a clinical research professional. The domain model here is derived from
> the public ICH E6(R3) guideline and from protocol and statistical analysis plan
> documents published on clinicaltrials.gov. All data is synthetic. Classification
> thresholds are illustrative and would require validation by a qualified clinical
> team before any real use. What is demonstrated is the oversight architecture:
> deterministic detection, surfaced ambiguity, and a human before every single
> write.

**All the data is invented.** There is not one real data point about one real
patient in it. A generator produces the sample study deliberately containing the
traps above.

**The thresholds are illustrative.** Exactly what percentage of dose deviation
counts as serious, or how many patients make a "systemic pattern" — I chose those
numbers; they are not professional consensus. Every decision states which
threshold drove it, so you can disagree with it specifically. Before any real use
they would need validation by qualified people.

**This is a demonstration, not a product.** No user authentication, no real
database, no connection to any existing clinical system.

What it demonstrates is not *"I can oversee a drug trial"*. It is **how you build
a system that could be used in a regulated setting**: predictable behaviour,
surfaced uncertainty, and a human before every single write.

---

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

## AWS Agent Core and Eval harness

This repository includes a small scaffold to run the agent as a worker that
consumes messages from AWS SQS and an evaluation harness for automated runs.

- `src/aws_agent_core_adapter.py` — a lightweight SQS poller that expects JSON
  messages like `{ "thread_id": "aws-1", "message": "review S-004" }`.
- `scripts/aws_agent_runner.py` — CLI wrapper to run the SQS worker.
- `scripts/eval_harness.py` — runs a set of sample prompts through the graph
  and prints timing and responses as JSON.

The adapter is intentionally minimal: it logs responses to stdout and deletes
messages after handling them. In production you may replace the print with an
SNS publish, write to S3, or push to a response queue and add dead-letter
handling on failures.

Run the eval harness quickly with:
```bash
python scripts/eval_harness.py
```

Run the AWS worker after installing `boto3` and setting AWS credentials and
`AWS_SQS_QUEUE_URL` (or pass `--queue-url`):
```bash
python scripts/aws_agent_runner.py --queue-url https://sqs....
```

## Sample use case — live with the included sample data

The repository contains deterministic sample study data in the `data/` folder
(see e.g. [data/subjects.json](data/subjects.json), [data/visits.json](data/visits.json),
and [data/vitals.json](data/vitals.json)). There are a few quick ways to exercise
the system with those samples.

1) Run the evaluation harness (fast, non-interactive):

```bash
python scripts/generate_data.py   # populate the data/ folder (deterministic)
python reset_sandbox.py          # clear any previous proposals/sandbox state
python scripts/eval_harness.py   # runs example prompts and prints JSON responses
```

The harness runs prompts such as `review S-004`, `review S-009` and
`review SITE-02` against the sample data and prints timings and assistant
responses. These subjects (`S-004`, `S-009`, `S-005`) exist in the sample set
so the output is live immediately.

2) Run the interactive UI and try the same prompts visually:

```bash
streamlit run app.py
```

Use the chat input or the sidebar examples (try `review S-004`) to see drafted
actions, proposed classifications, and the approval card that requires a
human verbatim confirmation before any write.

3) Simulate operational use with the SQS worker (send one JSON message):

Example message body to send to your SQS queue:

```json
{"thread_id": "sample-1", "message": "review S-004"}
```

Run the worker to consume the message and print the agent response:

```bash
python scripts/aws_agent_runner.py --queue-url https://sqs.REGION.amazonaws.com/123456789012/your-queue
```

Notes:
- Use `python scripts/diff_findings.py` to compare detector output against the
  planted ground truth in `docs/data_traps.json`.
- The eval harness and the UI use the same compiled graph and deterministic
  detectors, so results are consistent between modes.


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
normalised key. An approximate retrieval miss does not merely lose a row here: it
**fabricates a safety finding, or hides one**. The key concepts registry is small, closed and known at build time;
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
14 days, plausible weight 30–250 kg. They are the weakest part of the system and
the part a clinical team would replace first, so they are collected in
[src/thresholds.py](src/thresholds.py) rather than scattered through the
detectors — each with its basis and what changes if it moves. A finding that
depends on one **names it** (`threshold_applied`) and quotes its provenance, so a
reader can disagree precisely rather than in general. Override with a
`thresholds.json` beside the data.

**A finding separates what is filed from what is explained.** `calculation` is
terse and factual — figures, record IDs, the comparison — and is what goes into
the record. `rationale` carries why it matters and what a naive reading gets
wrong, and is for the reviewer. *Given up:* the filed text no longer argues its
own case. *Bought:* a monitor reading fifty deviation entries is not reading
fifty essays.

**One site query per visit occasion, not one per detector.** A partial date is
noticed by the timing, dose and consent detectors, each of which would raise its
own query about the same event. The findings stay separate — each keeps its own
verdict and reasoning — but the query action is consolidated onto one of them.
*Given up:* a detector's output no longer maps one-to-one onto an outbound
action. *Bought:* the site receives one question about one visit.

**Site-level observations carry no verdict.** A verdict states whether one record
complied with the protocol; "SITE-03 enters data 40 days late" makes no such
claim, so its `verdict` is `None`. Otherwise the rule *compliance is the absence
of a finding* would be quietly contradicted by a stream of `COMPLIANT` findings.

**15 subjects, not 12.** Twelve cannot carry twelve traps, a four-subject
systemic pattern and a four-subject clean control group. A dataset where
everything is a finding proves nothing.

**A JSON sandbox, not a clinical database.** Authentication, multi-user
authorisation, durable transactions, EDC integration and an electronic signature
compliant with 21 CFR Part 11 are all out of scope. The approval gate here
records who approved and what they typed; it does not authenticate them.

**The ground truth is not a fully independent oracle, and cannot be made one
here.** `docs/DATA_TRAPS.md` and `docs/data_traps.json` are rendered from the
same registry the data is generated from, so they cannot drift — but they also
cannot *disagree*: plant the wrong thing and the documentation describes the
wrong thing confidently. That is exactly how three defects survived Phase 1. The
partial mitigation is a verification pass that re-opens the written files and
checks every claim the registry makes — that each named record exists, each named
subject is enrolled, and each subject declared clean really carries no Layer A
damage. It stops the registry asserting something the data does not contain. It
does not stop the registry and the data being wrong together.

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
