"""Groq, via its OpenAI-compatible API (https://api.groq.com/openai/v1).

Free-tier specifics handled here:
* 429 carries ``retry-after`` (seconds) → retryable with that countdown.
* A single request larger than the tokens-per-minute limit is rejected with 413 → retrying the same
  prompt can never succeed, so it's non-retryable with an actionable message.
* gpt-oss models are reasoning models: reasoning is dropped from the output and effort kept low, since
  reasoning tokens count against the output budget and the per-minute token limit.
"""

from typing import Any

import openai

from app.llm.adapter import LLMError
from app.llm.openai_provider import OpenAICompatibleProvider


class GroqProvider(OpenAICompatibleProvider):
    name = "groq"
    api_key_env = "GROQ_API_KEY"

    def __init__(
        self,
        api_key: str | None,
        model: str,
        *,
        base_url: str = "https://api.groq.com/openai/v1",
        reasoning_effort: str = "low",
        client: openai.OpenAI | None = None,
    ):
        super().__init__(api_key, model, base_url=base_url, client=client)
        self.reasoning_effort = reasoning_effort

    def _extra_params(self) -> dict[str, Any]:
        if not self.model.startswith("openai/gpt-oss"):
            return {}
        return {
            "reasoning_effort": self.reasoning_effort,
            "extra_body": {"include_reasoning": False},
        }

    def _status_error(self, exc: openai.APIStatusError) -> LLMError:
        if exc.status_code == 413:
            return LLMError(
                "groq: request exceeds the model's tokens-per-minute limit for your plan; "
                "lower GROQ_MAX_PROMPT_CHARS / GROQ_MAX_OUTPUT_TOKENS",
                retryable=False,
            )
        if exc.status_code == 404:
            return LLMError(
                f"groq: model '{self.model}' is unavailable (deprecated or not on your plan); set GROQ_MODEL",
                retryable=False,
            )
        return super()._status_error(exc)
