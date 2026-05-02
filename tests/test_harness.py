"""
Pytest-compatible test suite for Loophole regression testing.

Run with:
    pytest tests/test_harness.py -v
    pytest tests/test_harness.py -v -k "billing_fraud or whistleblower"  # filter by name
    pytest tests/test_harness.py -v --tb=short  # short tracebacks

Requires LITELLM_API_KEY and/or LITELLM_API_BASE env vars (or config.yaml in the loophole root).
"""
from __future__ import annotations

import pytest
from loophole.agents.test_harness import (
    DEFAULT_SUITE,
    TestCase,
    run_test_case,
    run_test_suite,
    SuiteResult,
)


class TestLoopholeHarness:
    """Smoke tests for the Loophole self-testing harness itself."""

    def test_harness_loads(self):
        """Test harness module loads without errors."""
        from loophole.agents import test_harness
        assert hasattr(test_harness, "run_test_case")
        assert hasattr(test_harness, "run_test_suite")
        assert hasattr(test_harness, "DEFAULT_SUITE")

    def test_default_suite_has_cases(self):
        """Default suite must have at least one test case."""
        assert len(DEFAULT_SUITE) >= 1

    def test_test_case_dataclass(self):
        """TestCase dataclass builds correctly."""
        tc = TestCase(
            name="test_case",
            scenario="Something happens.",
            moral_principles="Do the right thing.",
            domain="Test",
        )
        assert tc.name == "test_case"
        assert tc.expected_case_type is None

    def test_suite_result_aggregation(self):
        """SuiteResult aggregates counts correctly."""
        from loophole.agents.test_harness import TestResult, SuiteResult

        r1 = TestResult(
            test_name="t1", scenario="s1",
            moral_principles="p1", domain="d1", passed=True,
        )
        r2 = TestResult(
            test_name="t2", scenario="s2",
            moral_principles="p2", domain="d2", passed=False,
        )
        suite = SuiteResult(name="test", results=[r1, r2])
        assert suite.passed_count == 1
        assert suite.failed_count == 1

    def test_test_result_summary(self):
        """TestResult.summary() produces a non-empty string."""
        r = TestResult(
            test_name="t1", scenario="s1",
            moral_principles="p1", domain="d1", passed=True,
        )
        s = r.summary()
        assert isinstance(s, str)
        assert len(s) > 0
        assert "PASS" in s

    def test_test_result_as_dict(self):
        """TestResult.as_dict() serializes correctly."""
        r = TestResult(
            test_name="t1", scenario="s1",
            moral_principles="p1", domain="d1", passed=True,
        )
        d = r.as_dict()
        assert isinstance(d, dict)
        assert d["test_name"] == "t1"
        assert d["passed"] is True
        assert "agent_results" in d


class TestLoopholeSmokeSuite:
    """
    End-to-end smoke tests: run each default test case and assert
    it completes without throwing an exception.

    These are not assertions about correctness of the moral debate —
    they verify that the pipeline executes end-to-end without crashing.
    """

    @pytest.mark.parametrize("test_case", DEFAULT_SUITE, ids=lambda tc: tc.name)
    def test_case_runs_without_exception(self, test_case: TestCase):
        """Each default test case must run without raising an exception."""
        # Should not raise — any exception = test failure
        result = run_test_case(test_case)
        # Error field should be None (no crash)
        assert result.error is None, f"Ran with error: {result.error}"

    @pytest.mark.parametrize("test_case", DEFAULT_SUITE, ids=lambda tc: tc.name)
    def test_case_creates_code(self, test_case: TestCase):
        """Legislator must produce a legal code for every test case."""
        result = run_test_case(test_case)
        assert result.agent_results.get("legislator") is not None, "Legislator did not run"
        assert result.agent_results["legislator"].passed is True, "Legislator failed"
        assert result.code_version >= 1, f"Code version should be >= 1, got {result.code_version}"
        assert len(result.code_text) > 10, "Code text too short"

    @pytest.mark.parametrize("test_case", DEFAULT_SUITE, ids=lambda tc: tc.name)
    def test_case_produces_cases(self, test_case: TestCase):
        """At least one agent (loophole or overreach) must find cases."""
        result = run_test_case(test_case)
        loophole = result.agent_results.get("loophole")
        overreach = result.agent_results.get("overreach")
        assert loophole is not None or overreach is not None, "Neither agent ran"
        total_proposed = (len(loophole.reasoning) if loophole else 0) + (
            len(overreach.reasoning) if overreach else 0
        )
        assert result.cases_created > 0, "No cases were created"

    @pytest.mark.parametrize("test_case", DEFAULT_SUITE, ids=lambda tc: tc.name)
    def test_judge_runs(self, test_case: TestCase):
        """Judge must complete for every test case."""
        result = run_test_case(test_case)
        assert result.judge_report is not None, "Judge did not produce a report"
        assert result.judge_report.error is None, f"Judge error: {result.judge_report.error}"
        assert result.judge_report.verdict in (
            "resolvable", "unresolvable"
        ), f"Unexpected verdict: {result.judge_report.verdict}"

    @pytest.mark.parametrize("test_case", DEFAULT_SUITE, ids=lambda tc: tc.name)
    def test_no_hard_coded_api_keys(self, test_case: TestCase):
        """Verify LITELLM_API_KEY is not hardcoded in config or test case."""
        # This is a static check: the test case moral_principles should not
        # accidentally contain the string "sk-"
        assert "sk-" not in test_case.moral_principles
        assert "sk-" not in test_case.scenario


class TestLoopholeRegressionSuite:
    """
    Regression assertions. These check that agent behaviour is consistent
    between commits — the bar for "pass" is intentionally low (no crash,
    produces output) so that the harness catches real breakage, not
    subjective quality judgments.
    """

    def test_suite_passes_overall(self):
        """The full default suite should produce no crashes."""
        suite = run_test_suite("regression", DEFAULT_SUITE)
        # At least half should pass (no crash = pass)
        assert suite.passed_count >= len(suite.results) // 2, (
            f"Too many failures: {suite.failed_count}/{len(suite.results)}"
        )

    def test_all_cases_resolve_without_error(self):
        """Zero cases should error out entirely."""
        suite = run_test_suite("regression", DEFAULT_SUITE)
        errored = [r for r in suite.results if r.error is not None]
        assert len(errored) == 0, (
            f" {len(errored)} case(s) errored: {[r.test_name for r in errored]}"
        )

    def test_token_budget_reasonable(self):
        """No single test case should blow past 500k tokens (~chars/4 * 4)."""
        suite = run_test_suite("regression", DEFAULT_SUITE)
        for r in suite.results:
            assert r.total_tokens < 500_000, (
                f"Case {r.test_name} used {r.total_tokens} tokens — exceeds reasonable budget"
            )

    def test_duration_reasonable(self):
        """No single test case should take more than 5 minutes."""
        for tc in DEFAULT_SUITE:
            result = run_test_case(tc)
            assert result.total_duration_ms < 5 * 60 * 1000, (
                f"Case {tc.name} took {result.total_duration_ms}ms — exceeds 5min budget"
            )
