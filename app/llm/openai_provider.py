from collections.abc import Iterator

import openai

from app.llm.adapter import LLMError, LLMEvent, LLMRequest, TextDelta, Usage


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str | None, model: str, client: openai.OpenAI | None = None):
        if client is None and not api_key:
            raise LLMError("OPENAI_API_KEY is not set", retryable=False)
        self.model = model
        self._client = client or openai.OpenAI(api_key=api_key, max_retries=0)

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
            )
            for chunk in chunks:
                if chunk.usage is not None:
                    usage = Usage(chunk.usage.prompt_tokens, chunk.usage.completion_tokens)
                for choice in chunk.choices:
                    if choice.delta and choice.delta.content:
                        yield TextDelta(choice.delta.content)
        except (
            openai.RateLimitError,
            openai.InternalServerError,
            openai.APIConnectionError,
        ) as exc:
            raise LLMError(f"openai: {exc}", retryable=True) from exc
        except openai.APIStatusError as exc:
            raise LLMError(f"openai: {exc}", retryable=exc.status_code in (408, 409)) from exc
        yield usage
