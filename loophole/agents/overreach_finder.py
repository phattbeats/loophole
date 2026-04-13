from __future__ import annotations

import re
from typing import Any

from loophole.agents.base import BaseAgent
from loophole.models import Case, CaseType, RoundType, SessionState
from loophole.prompts import (
    OVERREACH_FINDER_SYSTEM,
    OVERREACH_FINDER_SYSTEM_OPENING,
    OVERREACH_FINDER_SYSTEM_ATTACK,
    OVERREACH_FINDER_SYSTEM_CLOSING,
    OVERREACH_FINDER_USER,
)


ROUND_TYPE_INSTRUCTIONS = {
    RoundType.OPENING: OVERREACH_FINDER_SYSTEM_OPENING,
    RoundType.ATTACK: OVERREACH_FINDER_SYSTEM_ATTACK,
    RoundType.CLOSING: OVERREACH_FINDER_SYSTEM_CLOSING,
}


def _format_prior_cases(cases: list[Case]) -> str:
    if not cases:
        return "(none yet)"
    parts = []
    for c in cases:
        parts.append(f"Case #{c.id} ({c.case_type.value}): {c.scenario}")
    return "\n".join(parts)


class OverreachFinder(BaseAgent):
    def __init__(self, *args: Any, cases_per_agent: int = 3, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.cases_per_agent = cases_per_agent

    def _build_system_prompt(self, round_type: RoundType = RoundType.OPENING, **kwargs: Any) -> str:
        instruction = ROUND_TYPE_INSTRUCTIONS.get(round_type, OVERREACH_FINDER_SYSTEM_OPENING)
        return OVERREACH_FINDER_SYSTEM.format(
            cases_per_agent=self.cases_per_agent,
            round_type=round_type.value.upper(),
            round_type_instruction=instruction,
        )

    def _build_user_message(self, state: SessionState, **kwargs: Any) -> str:
        return OVERREACH_FINDER_USER.format(
            moral_principles=state.moral_principles,
            user_clarifications="\n".join(state.user_clarifications) or "(none)",
            code_version=state.current_code.version,
            legal_code=state.current_code.text,
            prior_cases_text=_format_prior_cases(state.cases),
            cases_per_agent=self.cases_per_agent,
        )

    def find(self, state: SessionState, round_type: RoundType = RoundType.OPENING) -> list[Case]:
        raw = self.run(state, round_type=round_type)
        return _parse_scenarios(raw, state, round_type)


def _parse_scenarios(raw: str, state: SessionState, round_type: RoundType = RoundType.OPENING) -> list[Case]:
    cases: list[Case] = []
    for m in re.finditer(
        r"<scenario>\s*<description>(.*?)</description>\s*<explanation>(.*?)</explanation>\s*</scenario>",
        raw,
        re.DOTALL,
    ):
        cases.append(
            Case(
                id=state.next_case_id + len(cases),
                round=state.current_round,
                round_type=round_type,
                case_type=CaseType.OVERREACH,
                scenario=m.group(1).strip(),
                explanation=m.group(2).strip(),
            )
        )
    return cases