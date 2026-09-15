"""LLM provider tests.

The OpenAI SDK sends requests through its vendored ``httpx2`` fork, which HTTP-mocking libraries that
patch ``httpx`` do not intercept. Tests therefore inject a client with a MockTransport, which makes it
impossible for them to reach a real API.
"""

import json
from collections.abc import Callable

import httpx2
import openai
import pytest

from app.config import Settings
from app.llm import get_provider
from app.llm.adapter import LLMError, LLMRequest, TextDelta, Usage
from app.llm.groq_provider import GroqProvider

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
REQUEST = LLMRequest(system="sys", user="review this", max_tokens=500)


def sse(*events: dict | str) -> bytes:
    return "".join(
        f"data: {e if isinstance(e, str) else json.dumps(e)}\n\n" for e in events
    ).encode()


def chunk(
    content: str | None = None,
    *,
    usage: dict | None = None,
    x_groq: dict | None = None,
    finish: str | None = None,
) -> dict:
    choices = (
        []
        if content is None
        else [{"index": 0, "delta": {"content": content}, "finish_reason": finish}]
    )
    body: dict = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "openai/gpt-oss-120b",
        "choices": choices,
    }
    if usage is not None:
        body["usage"] = usage
    if x_groq is not None:
        body["x_groq"] = x_groq
    return body


class FakeGroq:
    """Records request bodies and answers with a canned response."""

    def __init__(self, respond: Callable[[], httpx2.Response]):
        self.respond = respond
        self.requests: list[dict] = []

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        assert str(request.url) == GROQ_URL
        self.requests.append(json.loads(request.content))
        return self.respond()

    def provider(self, model: str = "openai/gpt-oss-120b") -> GroqProvider:
        client = openai.OpenAI(
            api_key="gsk_test",
            base_url="https://api.groq.com/openai/v1",
            max_retries=0,
            http_client=httpx2.Client(transport=httpx2.MockTransport(self._handle)),
        )
        return GroqProvider("gsk_test", model, client=client)


def streaming(*events: dict | str) -> Callable[[], httpx2.Response]:
    return lambda: httpx2.Response(
        200, headers={"content-type": "text/event-stream"}, content=sse(*events)
    )


def error(
    status: int, message: str, headers: dict[str, str] | None = None
) -> Callable[[], httpx2.Response]:
    return lambda: httpx2.Response(
        status, headers=headers or {}, json={"error": {"message": message}}
    )


def test_groq_streams_text_and_reads_usage_chunk() -> None:
    groq = FakeGroq(
        streaming(
            chunk("### Summary\n"),
            chunk("Looks fine.", finish="stop"),
            chunk(usage={"prompt_tokens": 120, "completion_tokens": 7, "total_tokens": 127}),
            "[DONE]",
        )
    )
    events = list(groq.provider().stream(REQUEST))

    assert [e.text for e in events if isinstance(e, TextDelta)] == ["### Summary\n", "Looks fine."]
    assert events[-1] == Usage(120, 7)
    [sent] = groq.requests
    assert sent["model"] == "openai/gpt-oss-120b"
    assert sent["max_completion_tokens"] == 500
    assert sent["stream_options"] == {"include_usage": True}
    # Reasoning tokens would eat the tiny free-tier budget.
    assert sent["reasoning_effort"] == "low"
    assert sent["include_reasoning"] is False


def test_groq_usage_falls_back_to_x_groq_extension() -> None:
    usage = {"prompt_tokens": 50, "completion_tokens": 1}
    groq = FakeGroq(
        streaming(chunk("ok", finish="stop", x_groq={"id": "req_1", "usage": usage}), "[DONE]")
    )
    assert list(groq.provider().stream(REQUEST))[-1] == Usage(50, 1)


def test_non_reasoning_models_get_no_reasoning_params() -> None:
    groq = FakeGroq(streaming(chunk("x"), "[DONE]"))
    list(groq.provider("qwen/qwen3.6-27b").stream(REQUEST))
    [sent] = groq.requests
    assert "reasoning_effort" not in sent and "include_reasoning" not in sent


def test_groq_429_is_retryable_with_retry_after() -> None:
    groq = FakeGroq(error(429, "Rate limit reached", {"retry-after": "7"}))
    with pytest.raises(LLMError) as caught:
        list(groq.provider().stream(REQUEST))
    assert caught.value.retryable and caught.value.rate_limited
    assert caught.value.retry_after == 7.0


def test_groq_413_request_too_large_is_not_retried() -> None:
    groq = FakeGroq(
        error(413, "Request too large on tokens per minute (TPM): Limit 8000, Requested 9100")
    )
    with pytest.raises(LLMError, match="GROQ_MAX_PROMPT_CHARS") as caught:
        list(groq.provider().stream(REQUEST))
    assert not caught.value.retryable


def test_groq_unknown_model_is_not_retried() -> None:
    groq = FakeGroq(error(404, "The model `llama-3.3-70b-versatile` does not exist"))
    with pytest.raises(LLMError, match="GROQ_MODEL") as caught:
        list(groq.provider().stream(REQUEST))
    assert not caught.value.retryable


def test_groq_5xx_is_retryable() -> None:
    groq = FakeGroq(error(503, "unavailable"))
    with pytest.raises(LLMError) as caught:
        list(groq.provider().stream(REQUEST))
    assert caught.value.retryable and not caught.value.rate_limited


def test_groq_requires_api_key() -> None:
    with pytest.raises(LLMError, match="GROQ_API_KEY"):
        GroqProvider(None, "openai/gpt-oss-120b")


def test_factory_builds_groq_with_free_tier_budgets() -> None:
    settings = Settings(_env_file=None, llm_provider="groq", groq_api_key="gsk_test")
    llm = get_provider(settings)
    assert isinstance(llm, GroqProvider)
    assert (llm.name, llm.model) == ("groq", "openai/gpt-oss-120b")
    assert settings.effective_max_prompt_chars == 12_000
    assert settings.effective_max_output_tokens == 1_800
