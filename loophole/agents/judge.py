from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from loophole.agents.base import BaseAgent
from loophole.models import Case, RoundType, SessionState
from loophole.prompts import (
    JUDGE_RESOLVE,
    JUDGE_SYSTEM,
    JUDGE_SYSTEM_CLOSING,
    JUDGE_SYSTEM_DEFAULT,
    JUDGE_VALIDATE,
)


def _format_resolved_cases(state: SessionState) -> str:
    parts = []
    if state.case_summaries:
        parts.append("Summarized earlier cases (concise):")
        parts.extend(state.case_summaries)
        parts.append("")
    full_cases = state.resolved_cases
    if not full_cases:
        if not parts:
            return "(none yet)"
        return "\n".join(parts)
    for c in full_cases:
        parts.append(
            f"Case #{c.id} ({c.case_type.value})\n"
            f"  Scenario: {c.scenario}\n"
            f"  Problem: {c.explanation}\n"
            f"  Resolution: {c.resolution}\n"
            f"  Resolved by: {c.resolved_by}"
        )
    return "\n\n".join(parts)


@dataclass
class JudgeResult:
    resolvable: bool
    reasoning: str
    proposed_revision: str | None = None
    resolution_summary: str | None = None
    conflict_explanation: str | None = None


@dataclass
class ValidationResult:
    passes: bool
    details: str


class Judge(BaseAgent):
    def _build_system_prompt(self, round_type: RoundType = RoundType.OPENING, **kwargs: Any) -> str:
        closing_instr = JUDGE_SYSTEM_CLOSING if round_type == RoundType.CLOSING else JUDGE_SYSTEM_DEFAULT
        return JUDGE_SYSTEM.format(
            round_type=round_type.value.upper(),
            closing_instruction=closing_instr,
        )

    def _build_user_message(self, state: SessionState, **kwargs: Any) -> str:
        case: Case = kwargs["case"]
        return JUDGE_RESOLVE.format(
            moral_principles=state.moral_principles,
            user_clarifications="\n".join(state.user_clarifications) or "(none)",
            code_version=state.current_code.version,
            legal_code=state.current_code.text,
            case_type=case.case_type.value,
            case_scenario=case.scenario,
            case_explanation=case.explanation,
            resolved_cases_text=_format_resolved_cases(state),
        )

    def evaluate(self, state: SessionState, case: Case, round_type: RoundType = RoundType.OPENING) -> JudgeResult:
        raw = self.run(state, round_type=round_type, case=case)

        verdict_match = re.search(r"<verdict>\s*(.*?)\s*</verdict>", raw, re.DOTALL)
        verdict = verdict_match.group(1).strip().lower() if verdict_match else "unresolvable"

        reasoning = _extract_tag(raw, "reasoning") or ""

        if verdict == "resolvable":
            return JudgeResult(
                resolvable=True,
                reasoning=reasoning,
                proposed_revision=_extract_tag(raw, "proposed_revision"),
                resolution_summary=_extract_tag(raw, "resolution_summary"),
            )
        return JudgeResult(
            resolvable=False,
            reasoning=reasoning,
            conflict_explanation=_extract_tag(raw, "conflict_explanation"),
        )

    def validate(self, state: SessionState, proposed_code: str) -> ValidationResult:
        resolved = state.resolved_cases
        if not resolved:
            return ValidationResult(passes=True, details="No prior cases to validate against.")

        user_msg = JUDGE_VALIDATE.format(
            proposed_code=proposed_code,
            resolved_cases_text=_format_resolved_cases(state),
        )
        raw = self.llm.call(JUDGE_SYSTEM.format(
            round_type="validation",
            closing_instruction="",
        ), user_msg, temperature=self.temperature)

        passes_match = re.search(r"<passes>\s*(.*?)\s*</passes>", raw, re.DOTALL)
        passes = passes_match.group(1).strip().lower() == "true" if passes_match else False

        details = _extract_tag(raw, "details") or raw
        return ValidationResult(passes=passes, details=details)


def _extract_tag(text: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
    return m.group(1).strip() if m else None