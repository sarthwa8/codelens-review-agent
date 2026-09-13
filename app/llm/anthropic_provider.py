from collections.abc import Iterator

import anthropic

from app.llm.adapter import LLMError, LLMEvent, LLMRequest, TextDelta, Usage


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str | None, model: str, client: anthropic.Anthropic | None = None):
        if client is None and not api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set", retryable=False)
        self.model = model
        # SDK-level retries are disabled: Celery owns retries so the UI can be told about them.
        self._client = client or anthropic.Anthropic(api_key=api_key, max_retries=0)

    def stream(self, request: LLMRequest) -> Iterator[LLMEvent]:
        try:
            with self._client.messages.stream(
                model=self.model,
                max_tokens=request.max_tokens,
                system=request.system,
                messages=[{"role": "user", "content": request.user}],
            ) as stream:
                for text in stream.text_stream:
                    yield TextDelta(text)
                final = stream.get_final_message()
            yield Usage(final.usage.input_tokens, final.usage.output_tokens)
        except (
            anthropic.RateLimitError,
            anthropic.InternalServerError,
            anthropic.APIConnectionError,
        ) as exc:
            raise LLMError(f"anthropic: {exc}", retryable=True) from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(
                f"anthropic: {exc}", retryable=exc.status_code in (408, 409, 529)
            ) from exc
