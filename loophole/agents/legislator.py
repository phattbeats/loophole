from __future__ import annotations

import re
from typing import Any

from loophole.agents.base import BaseAgent
from loophole.models import Case, LegalCode, SessionState
from loophole.prompts import LEGISLATOR_INITIAL, LEGISLATOR_REVISE, LEGISLATOR_SYSTEM


def _format_resolved_cases(cases: list[Case]) -> str:
    if not cases:
        return "(none yet)"
    parts = []
    for c in cases:
        parts.append(
            f"Case #{c.id} ({c.case_type.value}) — {c.scenario}\n"
            f"  Resolution: {c.resolution}\n"
            f"  Resolved by: {c.resolved_by}"
        )
    return "\n\n".join(parts)


class Legislator(BaseAgent):
    def _build_system_prompt(self, **kwargs: Any) -> str:
        return LEGISLATOR_SYSTEM

    def _build_user_message(self, state: SessionState, **kwargs: Any) -> str:
        case: Case | None = kwargs.get("case")
        if case is None:
            return LEGISLATOR_INITIAL.format(
                domain=state.domain,
                moral_principles=state.moral_principles,
            )
        return LEGISLATOR_REVISE.format(
            domain=state.domain,
            moral_principles=state.moral_principles,
            user_clarifications="\n".join(state.user_clarifications) or "(none)",
            code_version=state.current_code.version,
            legal_code=state.current_code.text,
            case_type=case.case_type.value,
            case_scenario=case.scenario,
            case_explanation=case.explanation,
            case_resolution=case.resolution,
            resolved_cases_text=_format_resolved_cases(state.resolved_cases),
        )

    def draft_initial(self, state: SessionState) -> LegalCode:
        raw = self.run(state)
        text = _extract_tag(raw, "legal_code") or raw
        return LegalCode(version=1, text=text.strip())

    def revise(self, state: SessionState, case: Case) -> LegalCode:
        raw = self.run(state, case=case)
        text = _extract_tag(raw, "legal_code") or raw
        changelog = _extract_tag(raw, "changelog")
        return LegalCode(
            version=state.current_code.version + 1,
            text=text.strip(),
            changelog=changelog,
        )


def _extract_tag(text: str, tag: str) -> str | None:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
    return m.group(1).strip() if m else None
