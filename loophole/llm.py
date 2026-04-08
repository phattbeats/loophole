from __future__ import annotations

from openai import OpenAI

from loophole.cost_tracker import get_tracker


class LLMClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        max_tokens: int = 4096,
        role: str = "unknown",
    ):
        """
        OpenAI-compatible LLM client.

        Args:
            base_url: Base URL for OpenAI-compatible endpoint (e.g. LiteLLM proxy)
            api_key: API key (any string for local proxies; real key for cloud)
            model: Model identifier (e.g. gpt-4o, claude-3-5-sonnet via LiteLLM)
            max_tokens: Maximum tokens to generate
            role: Agent role name used for cost tracking (e.g. "legislator", "judge")
        """
        self.client = OpenAI(base_url=base_url, api_key=api_key)
        self.model = model
        self.max_tokens = max_tokens
        self.role = role

    def call(self, system: str, user_message: str, temperature: float = 0.5) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_message},
            ],
            temperature=temperature,
            max_tokens=self.max_tokens,
        )
        # Record cost
        try:
            tracker = get_tracker()
            tracker.record(
                agent_role=self.role,
                model=response.model or self.model,
                input_tokens=response.usage.prompt_tokens,
                output_tokens=response.usage.completion_tokens,
                session_id="",  # filled by tracker if session is active
            )
        except Exception as e:
            # Never let cost tracking break an LLM call
            print(f"[COST] Tracking error: {e}")
        return response.choices[0].message.content
