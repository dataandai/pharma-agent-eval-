# Revenue Leakage Agent — Mock Prototype

A small, testable implementation of the challenge architecture:

- stateful LangGraph conversation routing;
- deterministic plan/invoice reconciliation;
- strict Pydantic v2 contracts;
- Make-Good Invoice, Credit Memo, and Plan Amendment proposals;
- explicit approval before mutation;
- proposal hash and sandbox-version checks;
- idempotent execution;
- atomic JSON writes;
- post-execution reconciliation;
- compensating rollback;
- append-only, hash-chained, searchable audit log.

## Trust boundary

```text
Conversation interpreter
→ strict structured intent
→ deterministic financial tools
→ immutable proposal
→ explicit approval
→ deterministic executor
→ verification
→ audit event
```

The LLM boundary classifies intent only; it never produces a write payload or a figure. Two interpreters implement it behind one interface: `LLMInterpreter` (Claude Haiku 4.5) and `RuleBasedInterpreter` (offline). `default_interpreter()` picks the LLM when `ANTHROPIC_API_KEY` is set and stays offline otherwise, so constructing the graph never requires credentials. Both are injectable via `build_agent_graph(root, interpreter=...)`; the test suite injects the rule-based one, so tests are deterministic and make no network calls. A malformed or failed LLM response degrades to the rule-based classifier rather than raising into the graph.

## Mock scenarios

- `P-100`: missing October invoice
- `P-200`: overbilling already corrected by an existing Credit Memo
- `P-300`: February underbilling
- `P-400`: January and February overbilling
- `P-500`: invoice matches documented contract-change evidence, producing a Plan Amendment candidate

## Run

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python reset_sandbox.py
pytest -q
streamlit run app.py
```

Offline CLI demo:

```bash
python demo.py
```

## Suggested chat commands

```text
investigate P-100
What currency is that plan in?
approve ACT-...
What happened to P-100?
rollback ACT-...
```

## Design limits

This is a challenge-scale JSON sandbox, not a production ledger. Authentication, multi-user authorization, durable database transactions, and external accounting integration are intentionally out of scope.
