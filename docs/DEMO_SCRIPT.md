# Demo script

Four moments, in order. The last one is the closer.

Run `python demo.py` for the whole thing offline, or `streamlit run app.py` to
show the approval card.

---

## 1. A routine finding, and the honest half of the answer

```
review S-013
```

One deviation and **six records the agent will not assess**. That second number
is the point of the demo, not an apology for it.

> Week 12: target 2025-08-25 +/-3 = 2025-08-22 to 2025-08-28; recorded as
> '2025-10' (month precision: 2025-10-01 to 2025-10-31); every day in that range
> falls outside the window, so the verdict does not depend on the missing detail.

Note what just happened: the date is imprecise and the verdict still holds,
because the whole recorded month sits outside the window. A system that treats
every partial date as unassessable is as wrong as one that imputes a day.

Then contrast it with the ones it refuses:

> Week 4: ... recorded as '2025-06' (month precision: 2025-06-01 to 2025-06-30);
> the range straddles the window edge, so the verdict would depend on
> information the record does not carry.

And a dose it will not compute:

> A weight was recorded on that date (VT-0094, value '73.3', no unit field), but
> it cannot be used: the weight has no unit, and assuming kilograms is not safe
> at a site that records pounds.

**The line to say out loud:** *"I can assess these; for those I cannot, and here
is the query I'd raise."*

---

## 2. The version pair — same lateness, opposite verdict

```
review S-004
review S-009
```

Both subjects' Week 4 visit is **exactly four days late**. S-004 is a deviation.
S-009 is not, and produces no finding at all.

> S-004 consented on 2025-06-20 under v1.0, so v1.0 governs them for the whole
> study. v2.0 was in force on 2025-08-08 (effective 2025-07-01), but the version
> in force on the calendar date does not govern a subject who consented earlier.
> Measuring S-004 against v2.0 would change the verdict without any change in
> what the site did.

S-004 consented eleven days before the amendment took effect and their Week 4
visit lands in August, when v2.0 is live. They keep v1.0's ±3 window. S-009
consented after and gets ±5.

**Both error directions are real:** measuring S-004 against v2.0 suppresses a
genuine deviation; measuring S-009 against v1.0 fabricates one. Nothing in the
calendar arithmetic distinguishes them — only the consent record does.

---

## 3. The systemic pattern — one amendment, not four reports

```
review SITE-02
```

> 4 of 5 enrolled subjects at SITE-02 (80%) fell outside the Week 4 window:
> S-005, S-006, S-008, S-010. ... A window missed by most of a site's subjects
> indicates the window is too tight for the site's operating conditions, not that
> 4 separate errors occurred. Filing 4 deviation reports would be the wrong
> remediation and would hide the real finding.

The proposed actions are `propose_protocol_amendment` and `open_capa` — not four
deviation entries. The four individual findings still exist and are still
auditable; each is marked:

> `FND-0006` suppressed (systemic_pattern) — not filed again.

**The point:** filing forty deviation reports is not just noise. It is the wrong
remediation, and it buries the finding that matters. Note also that the sponsor
already knew — v2.0 widened this exact window to ±5. The remaining v1.0 subjects
are still generating deviations against the old one.

---

## 4. The closer — a deviation that must not produce a CAPA

```
review S-005
```

S-005's Week 8 dose was withheld by the investigator.

> The protocol schedules a dose at this visit and permits holding only for:
> ANC < 0.5 x10^9/L, Platelets < 50 x10^9/L, Grade 4 non-haematological toxicity.
> A withheld dose that does not meet a stated hold criterion is a departure from
> the protocol and is therefore a deviation, which must be documented.
> ICH E6(R3) Section 2.5.3 permits a deviation made to eliminate an immediate
> hazard to the trial participant; where that applies, the deviation is
> documented but does **NOT** warrant a corrective action against the site.

The participant had grade 3 neutropenia (ANC 0.8) — *below* the protocol's
threshold for holding. So withholding genuinely departed from the protocol. And
it was obviously the right thing to do.

The proposed actions are `log_deviation` and `escalate_to_medical_monitor`.
**There is no `open_capa`.** A naive system opens a corrective action against a
site that did exactly the right thing.

Note also what the agent did *not* do:

> A clinical rationale is recorded on this record and is quoted below for the
> investigator and medical monitor to confirm — it is deliberately not parsed to
> reach this verdict.

The *coded* `dose_status` field drove the routing. The clinical note is quoted as
evidence, never parsed. Whether §2.5.3 applies is a clinical judgement, and the
system does not pretend to make it.

### Then show the gate refusing you

```
approve ACT-...          (the log_deviation action)
```

The graph pauses and shows the approval card: subject, visit, **the protocol
version governing that subject**, the calculation, the proposed classification
with its reasoning, and the exact file that will be written.

Answer `yes`:

> Not applied. This action requires the exact confirmation token because it
> proposes an important deviation, which has downstream regulatory effect. Reply
> with exactly: APPROVE ACT-...

Nothing was written. Then type the token, and check what was recorded:

```
what happened?
```

The audit entry carries the finding, the proposed classification, its reasoning,
the calculation, who approved, and **the approval verbatim** — not a boolean.
Paraphrasing a human's approval into `approved: true` is how accountability gets
lost.

---

## If someone pushes back

**"Why not let it classify automatically?"** E6(R3) places the review of
deviations on the investigator. A deviation record goes into the trial master
file and, depending on classification, to the IRB/EC. An auto-filed important
deviation has regulatory consequences; a misclassified one can bury a safety
signal. The gate is not a design preference here, it is the regulation.

**"Your thresholds are made up."** Yes — six of them, and they are stated in the
finding text so you can see exactly what drove each answer. A qualified clinical
team would set them. That is the honest version of a portfolio piece.

**"Why no RAG?"** An approximate retrieval miss here fabricates a safety finding
or hides one. The data is structured and the concept set is closed; exact
indexing is both simpler and correct.
