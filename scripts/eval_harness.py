"""Real evaluation harness with ground-truth assertions.

Unlike a scaffold demo, this harness:
  1. Runs the agent against predefined test cases
  2. Compares outputs against expected behavior (ground truth)
  3. Tracks metrics (response time, accuracy)
  4. Returns pass/fail verdicts usable for CI gating
  5. Produces machine-readable output for dashboards

The assertions here are non-negotiable: if they fail, the build should fail.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, asdict
from enum import Enum
from pathlib import Path
from typing import Any

# Ensure project root is importable as a top-level package path when invoked
# from scripts/ using a plain `python scripts/...` call.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.graph.builder import build_agent_graph, initial_state, run_turn
from src.interpreter import Intent


class TestResult(Enum):
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"


@dataclass
class AssertionError:
    """Assertion failure details for reporting."""
    assertion: str
    expected: Any
    actual: Any
    message: str = ""


@dataclass
class EvalTestCase:
    """A single test case: input prompt, expected behavior, assertions."""
    name: str
    prompt: str
    # Expected intent the classifier should produce
    expected_intent: Intent | None = None
    # Expected: at least N findings should be returned
    min_findings: int | None = None
    # Expected: response should be non-empty
    expect_response: bool = True
    # Expected: response should not contain these strings (red flags)
    reject_phrases: list[str] | None = None


# Ground-truth test cases derived from the protocol and data
TEST_CASES = [
    EvalTestCase(
        name="review_single_subject",
        prompt="review S-004",
        expected_intent=Intent.REVIEW,
        min_findings=0,  # S-004 may or may not have findings
        expect_response=True,
    ),
    EvalTestCase(
        name="review_site",
        prompt="review SITE-02",
        expected_intent=Intent.REVIEW,
        min_findings=0,
        expect_response=True,
    ),
    EvalTestCase(
        name="knowledge_question_on_deviation",
        prompt="what is an important deviation?",
        expected_intent=Intent.KNOWLEDGE_QUESTION,
        expect_response=True,
        reject_phrases=["REVIEW", "S-"],  # Should answer the question, not review
    ),
    EvalTestCase(
        name="audit_query",
        prompt="what happened?",
        expected_intent=Intent.AUDIT_QUERY,
        expect_response=True,
    ),
    EvalTestCase(
        name="reject_action_word_no",
        prompt="no",
        expected_intent=Intent.REJECT_ACTION,
        expect_response=True,
    ),
    EvalTestCase(
        name="approve_action",
        prompt="yes",
        expected_intent=Intent.APPROVE_ACTION,
        expect_response=True,
    ),
]


@dataclass
class EvalResult:
    """Results of a single test case evaluation."""
    test_name: str
    result: TestResult
    assertions_passed: int = 0
    assertions_failed: int = 0
    failures: list[AssertionError] | None = None
    response: str | None = None
    time_s: float = 0.0


def evaluate_single_case(graph, case: EvalTestCase) -> EvalResult:
    """Run a single test case and check assertions."""
    tid = f"eval-{case.name}"
    state = initial_state(tid)
    
    t0 = time.perf_counter()
    new_state = run_turn(graph, state, case.prompt)
    took = time.perf_counter() - t0
    
    response = new_state.get("response", "")
    failures = []
    passed = 0
    failed = 0
    
    # Assertion 1: expected_intent
    if case.expected_intent is not None:
        detected_intent = new_state.get("intent")
        if detected_intent == case.expected_intent:
            passed += 1
        else:
            failed += 1
            failures.append(AssertionError(
                assertion="intent_classification",
                expected=case.expected_intent,
                actual=detected_intent,
                message=f"Prompt '{case.prompt}' was classified as {detected_intent}, "
                        f"expected {case.expected_intent}"
            ))
    
    # Assertion 2: response non-empty
    if case.expect_response:
        if response and len(response.strip()) > 0:
            passed += 1
        else:
            failed += 1
            failures.append(AssertionError(
                assertion="response_nonempty",
                expected="response present",
                actual="empty or missing",
                message=f"Prompt '{case.prompt}' produced no response"
            ))
    
    # Assertion 3: min_findings
    if case.min_findings is not None:
        findings = new_state.get("findings", [])
        num_findings = len(findings)
        if num_findings >= case.min_findings:
            passed += 1
        else:
            failed += 1
            failures.append(AssertionError(
                assertion="min_findings",
                expected=case.min_findings,
                actual=num_findings,
                message=f"Expected at least {case.min_findings} findings, "
                        f"got {num_findings}"
            ))
    
    # Assertion 4: reject_phrases (response should NOT contain these)
    if case.reject_phrases:
        response_lower = response.lower() if response else ""
        bad_phrases = [p for p in case.reject_phrases if p.lower() in response_lower]
        if not bad_phrases:
            passed += 1
        else:
            failed += 1
            failures.append(AssertionError(
                assertion="reject_phrases",
                expected="none of " + str(case.reject_phrases),
                actual="found " + str(bad_phrases),
                message=f"Response should not contain {bad_phrases}"
            ))
    
    result = TestResult.FAIL if failures else TestResult.PASS
    return EvalResult(
        test_name=case.name,
        result=result,
        assertions_passed=passed,
        assertions_failed=failed,
        failures=failures if failures else None,
        response=response[:200] if response else None,  # truncate for output
        time_s=took,
    )


def run_eval(root: Path | str = None, cases: list[EvalTestCase] = None) -> dict:
    """Run full evaluation suite and return structured results."""
    if root is None:
        root = Path(__file__).resolve().parents[1]
    if cases is None:
        cases = TEST_CASES
    
    root = Path(root)
    graph = build_agent_graph(root)
    
    results = []
    for case in cases:
        try:
            result = evaluate_single_case(graph, case)
            results.append(result)
        except Exception as e:
            results.append(EvalResult(
                test_name=case.name,
                result=TestResult.FAIL,
                failures=[AssertionError(
                    assertion="exception",
                    expected="no exception",
                    actual=str(type(e).__name__),
                    message=str(e)
                )],
                time_s=0.0,
            ))
    
    # Aggregate results
    passed = sum(1 for r in results if r.result == TestResult.PASS)
    failed = sum(1 for r in results if r.result == TestResult.FAIL)
    total = len(results)
    
    return {
        "metadata": {
            "total_tests": total,
            "passed": passed,
            "failed": failed,
            "pass_rate": passed / total if total > 0 else 0.0,
        },
        "results": [asdict(r) for r in results],
    }


def main() -> None:
    """Run eval and exit with code 0 iff all tests pass."""
    root = Path(__file__).resolve().parents[1]
    output = run_eval(root)
    
    print(json.dumps(output, indent=2, ensure_ascii=False, default=str))
    
    # Return non-zero exit code if any test failed (CI gating)
    if output["metadata"]["failed"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
