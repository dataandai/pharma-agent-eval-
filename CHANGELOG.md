# Changelog

All notable changes to this project are documented in this file.

## Unreleased

- CI: add GitHub Actions workflow (`.github/workflows/ci.yml`) to run tests on
  push and pull requests (Python 3.10/3.11, installs `requirements.txt`, runs `pytest`).
- Fix: improve langgraph fallback compatibility (`src/graph/compat.py`):
  - added `get_graph()` / `get_state()` helpers for tests
  - accept `Command` objects for resume behavior
  - emulate pause/resume at the approval `gate`
- Fix: make `LLMInterpreter` tolerant when the `anthropic` package is not
  installed; falls back to the rule-based interpreter for local runs and tests
  (`src/interpreter.py`).
- Tests: updated and verified — full test suite passes locally (239 passed,
  1 skipped).
- Misc: added small debug helpers and demo outputs (`scripts/debug_*`,
  `scripts/eval_demo_output.json`).

---

For releases, create a tag (e.g. `v0.1.0`) and add release notes summarizing
changes from `Unreleased`.
