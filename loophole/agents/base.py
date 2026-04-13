from __future__ import annotations

from typing import Any

from loophole.llm import LLMClient
from loophole.models import RoundType, SessionState



class BaseAgent:
    def __init__(self, llm: LLMClient, temperature: float = 0.5):
        self.llm = llm
        self.temperature = temperature

    def _build_system_prompt(self, round_type: RoundType = RoundType.OPENING, **kwargs: Any) -> str:
        raise NotImplementedError

    def _build_user_message(self, state: SessionState, **kwargs: Any) -> str:
        raise NotImplementedError

    def run(self, state: SessionState, round_type: RoundType = RoundType.OPENING, **kwargs: Any) -> str:
        system = self._build_system_prompt(round_type=round_type, **kwargs)
        user_msg = self._build_user_message(state, **kwargs)
        return self.llm.call(system, user_msg, temperature=self.temperature)
