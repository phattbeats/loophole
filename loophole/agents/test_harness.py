"""
Test harness for Loophole — synthetic case runner + regression suite.

Spins up Loophole cases with synthetic moral dilemmas, runs the full debate
(legislator → loophole → overreach → judge → revise), and returns a structured
report: per-agent results, how the judge voted, token cost, run duration.

Usage:
    from loophole.agents.test_harness import run_test_case, run_test_suite, TestCase

    case = TestCase(
        scenario="A law firm bills clients for hours they didn't work.",
        moral_principles="Attorneys must be honest with clients. Billing for unworked hours is fraud.",
        domain="Legal Ethics",
    )
    result = run_test_case(case)
    print(result.summary())
"""
from __future__ import annotations
import time
import os
import re
import json
from dataclasses import dataclass, field
from typing import Optional

from loophole.llm import LLMClient
from loophole.models import CaseType, SessionState, LegalCode, Case, CaseStatus, RoundType
from loophole.agents.legislator import Legislator
from loophole.agents.loophole_finder import LoopholeFinder
from loophole.agents.overreach_finder import OverreachFinder
from loophole.agents.judge import Judge


# ─── Config ────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    """Load LiteLLM config from environment or config.yaml fallback."""
    import yaml
    from pathlib import Path

    config_path = Path(__file__).parent.parent.parent / "config.yaml"
    base = yaml.safe_load(config_path.read_text()) if config_path.exists() else {}

    lite_llm_cfg = base.get("lite_llm", {})
    base_url = os.getenv("LITELLM_API_BASE", lite_llm_cfg.get("base_url", "http://10.0.0.100:4000"))
    api_key = os.getenv("LITELLM_API_KEY", lite_llm_cfg.get("api_key", ""))
    default_model = os.getenv("DEFAULT_MODEL", lite_llm_cfg.get("default_model", "claude-3-5-sonnet-20241022"))
    max_tokens = base.get("model", {}).get("max_tokens", 4096)

    agent_models = base.get("agent_models", {})
    temperatures = base.get("temperatures", {})
    loop = base.get("loop", {"cases_per_agent": 3, "max_context_tokens": 60000})

    return {
        "base_url": base_url,
        "api_key": api_key,
        "default_model": default_model,
        "max_tokens": max_tokens,
        "agent_models": agent_models,
        "temperatures": temperatures,
        "cases_per_agent": loop.get("cases_per_agent", 3),
    }


def _make_client(role: str, config: dict) -> LLMClient:
    model = config["agent_models"].get(role, config["default_model"])
    return LLMClient(
        base_url=config["base_url"],
        api_key=config["api_key"],
        model=model,
        max_tokens=config["max_tokens"],
        role=role,
    )


def _make_agents(config: dict):
    """Build all four agents with their configured clients."""
    return {
        "legislator": Legislator(
            _make_client("legislator", config),
            temperature=config["temperatures"].get("legislator", 0.4),
        ),
        "loophole": LoopholeFinder(
            _make_client("loophole", config),
            temperature=config["temperatures"].get("loophole_finder", 0.9),
            cases_per_agent=config["cases_per_agent"],
        ),
        "overreach": OverreachFinder(
            _make_client("overreach", config),
            temperature=config["temperatures"].get("overreach_finder", 0.9),
            cases_per_agent=config["cases_per_agent"],
        ),
        "judge": Judge(
            _make_client("judge", config),
            temperature=config["temperatures"].get("judge", 0.3),
        ),
    }


# ─── Data models ───────────────────────────────────────────────────────────────

@dataclass
class AgentResult:
    agent: str
    passed: bool
    reasoning: str
    tokens_used: int = 0
    duration_ms: int = 0
    error: str | None = None


@dataclass
class JudgeReport:
    verdict: str  # "resolvable" | "unresolvable" | "error"
    reasoning: str
    cases_resolved: int = 0
    cases_escalated: int = 0
    tokens_used: int = 0
    duration_ms: int = 0
    error: str | None = None


@dataclass
class TestResult:
    test_name: str
    scenario: str
    moral_principles: str
    domain: str
    agent_results: dict[str, AgentResult] = field(default_factory=dict)
    judge_report: JudgeReport | None = None
    code_version: int = 0
    code_text: str = ""
    cases_created: int = 0
    cases_resolved: int = 0
    total_tokens: int = 0
    total_duration_ms: int = 0
    passed: bool = False
    error: str | None = None

    def summary(self) -> str:
        """One-line summary for pytest/logging."""
        status = "PASS" if self.passed else "FAIL"
        judge_v = self.judge_report.verdict if self.judge_report else "N/A"
        agents_ok = sum(1 for r in self.agent_results.values() if r.passed)
        agents_total = len(self.agent_results)
        return (
            f"[{status}] {self.test_name} | judge={judge_v} | "
            f"cases={self.cases_resolved}/{self.cases_created} | "
            f"agents={agents_ok}/{agents_total} | "
            f"tokens={self.total_tokens} | {self.total_duration_ms}ms"
        )

    def as_dict(self) -> dict:
        """Full report as dict for JSON serialization."""
        return {
            "test_name": self.test_name,
            "scenario": self.scenario,
            "moral_principles": self.moral_principles,
            "domain": self.domain,
            "passed": self.passed,
            "error": self.error,
            "total_tokens": self.total_tokens,
            "total_duration_ms": self.total_duration_ms,
            "code_version": self.code_version,
            "cases_created": self.cases_created,
            "cases_resolved": self.cases_resolved,
            "judge_verdict": self.judge_report.verdict if self.judge_report else None,
            "judge_reasoning": self.judge_report.reasoning if self.judge_report else None,
            "agent_results": {
                name: {
                    "passed": r.passed,
                    "reasoning": r.reasoning,
                    "tokens_used": r.tokens_used,
                    "duration_ms": r.duration_ms,
                    "error": r.error,
                }
                for name, r in self.agent_results.items()
            },
        }


@dataclass
class TestCase:
    """A single test case to run through the harness."""
    name: str
    scenario: str
    moral_principles: str
    domain: str = "General Ethics"
    expected_case_type: str | None = None  # "loophole" | "overreach" | None for either


@dataclass
class SuiteResult:
    name: str
    results: list[TestResult]
    total_tokens: int = 0
    total_duration_ms: int = 0

    @property
    def passed_count(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.results if not r.passed)

    def summary(self) -> str:
        return (
            f"Suite '{self.name}': {self.passed_count} passed, {self.failed_count} failed, "
            f"{self.total_tokens} tokens, {self.total_duration_ms}ms total"
        )


# ─── Token estimation ─────────────────────────────────────────────────────────

def _estimate_tokens(text: str) -> int:
    """Rough token estimate: chars / 4 (conservative)."""
    return (len(text or "") + 3) // 4


# ─── Core runner ──────────────────────────────────────────────────────────────

def run_test_case(test_case: TestCase, config: dict | None = None) -> TestResult:
    """
    Run a single test case through the full Loophole debate pipeline:
      1. Legislator drafts initial legal code
      2. LoopholeFinder proposes cases
      3. OverreachFinder proposes cases
      4. Judge resolves all cases (and triggers revise when resolvable)

    Returns a TestResult with per-agent results, judge report, and metrics.
    """
    if config is None:
        config = _load_config()

    agents = _make_agents(config)
    result = TestResult(
        test_name=test_case.name,
        scenario=test_case.scenario,
        moral_principles=test_case.moral_principles,
        domain=test_case.domain,
    )
    overall_start = time.time()

    legislator: Legislator = agents["legislator"]
    loophole_finder: LoopholeFinder = agents["loophole"]
    overreach_finder: OverreachFinder = agents["overreach"]
    judge: Judge = agents["judge"]

    try:
        # ── 1. Legislator drafts initial legal code ──────────────────────────────
        t0 = time.time()
        dummy_code = LegalCode(version=0, text="(drafting...)")
        state = SessionState(
            session_id=f"test_harness_{int(t0 * 1000)}",
            domain=test_case.domain,
            moral_principles=test_case.moral_principles,
            current_code=dummy_code,
        )

        initial_code = legislator.draft_initial(state)
        result.agent_results["legislator"] = AgentResult(
            agent="legislator",
            passed=True,
            reasoning=f"Drafted v{initial_code.version} ({len(initial_code.text)} chars)",
            tokens_used=_estimate_tokens(initial_code.text),
            duration_ms=int((time.time() - t0) * 1000),
        )
        state.current_code = initial_code

        # ── 2. LoopholeFinder proposes cases ───────────────────────────────────
        t0 = time.time()
        try:
            raw_loopholes = loophole_finder.find(state, round_type=RoundType.OPENING)
        except Exception as e:
            raw_loopholes = ""
            result.agent_results["loophole"] = AgentResult(
                agent="loophole", passed=False, reasoning="", error=str(e),
                duration_ms=int((time.time() - t0) * 1000),
            )
        if "loophole" not in result.agent_results:
            result.agent_results["loophole"] = AgentResult(
                agent="loophole",
                passed=True,
                reasoning=f"Proposed {len(raw_loopholes)} case(s)",
                tokens_used=_estimate_tokens(raw_loopholes or ""),
                duration_ms=int((time.time() - t0) * 1000),
            )

        # ── 3. OverreachFinder proposes cases ────────────────────────────────
        t0 = time.time()
        try:
            raw_overreach = overreach_finder.find(state, round_type=RoundType.OPENING)
        except Exception as e:
            raw_overreach = ""
            result.agent_results["overreach"] = AgentResult(
                agent="overreach", passed=False, reasoning="", error=str(e),
                duration_ms=int((time.time() - t0) * 1000),
            )
        if "overreach" not in result.agent_results:
            result.agent_results["overreach"] = AgentResult(
                agent="overreach",
                passed=True,
                reasoning=f"Proposed {len(raw_overreach)} case(s)",
                tokens_used=_estimate_tokens(raw_overreach or ""),
                duration_ms=int((time.time() - t0) * 1000),
            )

        result.cases_created = len(state.cases)

        # ── 4. Judge resolves all cases ────────────────────────────────────────
        t0 = time.time()
        resolved_count = 0
        escalated_count = 0
        judge_reasoning = ""

        try:
            for case_obj in state.cases:
                eval_result = judge.evaluate(state, case_obj, round_type=RoundType.OPENING)
                case_obj.round_type = RoundType.OPENING

                if eval_result.resolvable:
                    case_obj.resolution = eval_result.resolution_summary or eval_result.reasoning
                    case_obj.status = CaseStatus.AUTO_RESOLVED
                    case_obj.resolved_by = "judge"

                    # Legislator revises code based on this case
                    revised = legislator.revise(state, case_obj)
                    validation = judge.validate(state, revised.text)
                    if validation.passes:
                        state.current_code = revised
                        state.code_history.append(revised)
                        resolved_count += 1
                    else:
                        case_obj.status = CaseStatus.USER_RESOLVED
                        case_obj.resolved_by = "judge-auto"
                        escalated_count += 1
                else:
                    case_obj.status = CaseStatus.ESCALATED
                    case_obj.resolved_by = None
                    escalated_count += 1

                judge_reasoning += (
                    f"\nCase #{case_obj.id} ({case_obj.case_type.value}): "
                    f"{'resolvable' if eval_result.resolvable else 'unresolvable'} "
                    f"— {eval_result.reasoning[:100]}"
                )

        except Exception as e:
            judge_reasoning = f"Error during judge evaluation: {e}"

        judge_duration = int((time.time() - t0) * 1000)
        result.judge_report = JudgeReport(
            verdict="resolvable" if resolved_count > 0 else "unresolvable",
            reasoning=judge_reasoning.strip() or "(no reasoning captured)",
            cases_resolved=resolved_count,
            cases_escalated=escalated_count,
            duration_ms=judge_duration,
        )

        result.cases_resolved = resolved_count
        result.code_version = state.current_code.version
        result.code_text = state.current_code.text

        result.passed = (
            all(r.passed for r in result.agent_results.values())
            and result.judge_report.verdict != "error"
            and result.cases_created > 0
        )

    except Exception as e:
        result.error = str(e)
        result.passed = False

    result.total_duration_ms = int((time.time() - overall_start) * 1000)
    result.total_tokens = sum(r.tokens_used for r in result.agent_results.values())

    return result


def run_test_suite(suite_name: str, test_cases: list[TestCase]) -> SuiteResult:
    """
    Run a list of TestCases and aggregate results into a SuiteResult.
    """
    results = []
    overall_start = time.time()
    total_tokens = 0

    for tc in test_cases:
        result = run_test_case(tc)
        results.append(result)
        total_tokens += result.total_tokens

    return SuiteResult(
        name=suite_name,
        results=results,
        total_tokens=total_tokens,
        total_duration_ms=int((time.time() - overall_start) * 1000),
    )


# ─── Built-in test suites ─────────────────────────────────────────────────────

DEFAULT_SUITE = [
    TestCase(
        name="billing_fraud",
        scenario="A law firm bills clients for hours they didn't work, pocketing the extra revenue.",
        moral_principles="Attorneys must be honest with clients. Billing for unworked hours is fraud.",
        domain="Legal Ethics",
        expected_case_type="loophole",
    ),
    TestCase(
        name="whistleblower_retaliation",
        scenario="A corporation fires an employee who reported safety violations to the government.",
        moral_principles="Workers may not be retaliated against for reporting illegal activity. Safety violations must be reported.",
        domain="Employment Law",
        expected_case_type="overreach",
    ),
    TestCase(
        name="price_gouging",
        scenario="A pharmacy increases the price of a life-saving medication by 500% after a natural disaster.",
        moral_principles="Essential goods must be priced affordably. Exploiting desperate customers is unethical.",
        domain="Consumer Protection",
        expected_case_type="loophole",
    ),
    TestCase(
        name="student_privacy",
        scenario="A school monitors student social media accounts without consent to detect potential threats.",
        moral_principles="Students have a right to privacy. Surveillance must be proportionate and transparent.",
        domain="Privacy Law",
        expected_case_type="overreach",
    ),
    TestCase(
        name="algorithmic_bias",
        scenario="A hiring algorithm systematically disadvantages female candidates by training on historical data.",
        moral_principles="Hiring must be non-discriminatory. Algorithms must not perpetuate historical biases.",
        domain="Employment Discrimination",
        expected_case_type="loophole",
    ),
]


if __name__ == "__main__":
    # Quick CLI smoke test
    print("Running default test suite...")
    suite = run_test_suite("default", DEFAULT_SUITE)
    print(suite.summary())
    for r in suite.results:
        print(r.summary())