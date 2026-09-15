"""OpenAI Chat Completions streaming, reusable for any OpenAI-compatible API (e.g. Groq)."""

from collections.abc import Iterator
from typing import Any

import openai

from app.llm.adapter import LLMError, LLMEvent, LLMRequest, TextDelta, Usage


def _retry_after(exc: openai.APIStatusError) -> float | None:
    value = exc.response.headers.get("retry-after")
    try:
        return float(value) if value else None
    except ValueError:
        return None


def _usage_from_chunk(chunk: Any) -> Usage | None:
    if getattr(chunk, "usage", None) is not None:
        return Usage(chunk.usage.prompt_tokens, chunk.usage.completion_tokens)
    # Groq also reports usage in a vendor extension on the final chunk.
    extra = (getattr(chunk, "model_extra", None) or {}).get("x_groq") or {}
    usage = extra.get("usage") if isinstance(extra, dict) else None
    if usage:
        return Usage(usage.get("prompt_tokens"), usage.get("completion_tokens"))
    return None


class OpenAICompatibleProvider:
    name = "openai"
    api_key_env = "OPENAI_API_KEY"

    def __init__(
        self,
        api_key: str | None,
        model: str,
        *,
        base_url: str | None = None,
        client: openai.OpenAI | None = None,
    ):
        if client is None and not api_key:
            raise LLMError(f"{self.api_key_env} is not set", retryable=False)
        self.model = model
        # SDK retries are disabled: Celery owns retries so the UI can show "retrying".
        self._client = client or openai.OpenAI(api_key=api_key, base_url=base_url, max_retries=0)

    def _extra_params(self) -> dict[str, Any]:
        return {}

    def _status_error(self, exc: openai.APIStatusError) -> LLMError:
        return LLMError(f"{self.name}: {exc}", retryable=exc.status_code in (408, 409))

    def stream(self, request: LLMRequest) -> Iterator[LLMEvent]:
        usage = Usage(None, None)
        try:
            chunks = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": request.system},
                    {"role": "user", "content": request.user},
                ],
                max_completion_tokens=request.max_tokens,
                stream=True,
                stream_options={"include_usage": True},
                **self._extra_params(),
            )
            for chunk in chunks:
                usage = _usage_from_chunk(chunk) or usage
                # The usage chunk has an empty choices list.
                for choice in chunk.choices:
                    if choice.delta and choice.delta.content:
                        yield TextDelta(choice.delta.content)
        except openai.RateLimitError as exc:
            raise LLMError(
                f"{self.name}: rate limited ({exc})",
                retryable=True,
                retry_after=_retry_after(exc),
                rate_limited=True,
            ) from exc
        except (openai.InternalServerError, openai.APIConnectionError) as exc:
            raise LLMError(f"{self.name}: {exc}", retryable=True) from exc
        except openai.APIStatusError as exc:
            raise self._status_error(exc) from exc
        yield usage


class OpenAIProvider(OpenAICompatibleProvider):
    name = "openai"
    api_key_env = "OPENAI_API_KEY"
