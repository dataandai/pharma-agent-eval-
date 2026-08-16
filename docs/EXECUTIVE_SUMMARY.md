# Protocol Deviation Agent — Executive Summary

Purpose
- Reduce manual work and regulatory risk by automatically finding and
  explaining departures from a clinical trial protocol, then handing a clear
  decision and rationale to a named human reviewer.

Why agents
- Agents coordinate deterministic detectors (the "rules") and the
  conversational interface. The language model only interprets what the user
  is asking — it does not compute results — so every reported figure is
  produced by auditable code.

Key benefits
- Fewer false positives: data-quality problems are separated from protocol
  deviations.
- Faster reviews: monitors receive pre-computed findings with exact record IDs
  and calculations.
- Compliance and accountability: nothing is written to official ledgers until a
  named human approves; approvals are stored verbatim in a hash-chained audit trail.

How it is used (one-line):
- Run the included detectors on the sample data, review proposed actions in the
  UI or via the eval harness, and approve or reject each proposal manually.

Quick win (try in 5 minutes)
- `python -m venv .venv && .venv\Scripts\activate` (Windows)
- `pip install -r requirements.txt`
- `python scripts/generate_data.py && python reset_sandbox.py`
- `python scripts/eval_harness.py` — runs example prompts like `review S-004`

Operational notes for leadership
- The system is a demo scaffold: it uses JSON files (sandbox) rather than an
  enterprise EDC. It demonstrates an audit-friendly architecture, not a
  production EDC integration.
- For deployment, the worker can be run from AWS (SQS) or other orchestration
  platforms; the `src/aws_agent_core_adapter.py` is a minimal scaffold.

Decision points for adoption
- Validate and set domain thresholds (dose tolerance, late-window sizes).
- Integrate with the sponsor's EDC and electronic signature solution if used
  in production.
- Define who (roles) may approve: investigators, delegates, monitors.

Contact
- See the repository `README.md` and `ARCHITECTURE.md` for implementation and
  testing details, or ask the engineering team to run the eval harness and a
  Streamlit demo for a walkthrough.
