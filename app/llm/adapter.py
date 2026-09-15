"""Provider-agnostic LLM streaming interface.

Every provider yields a stream of events: ``TextDelta`` for each piece of generated text and a
single ``Usage`` at the end. Nothing else in the codebase imports a vendor SDK.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class LLMRequest:
    system: str
    user: str
    max_tokens: int


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class Usage:
    input_tokens: int | None
    output_tokens: int | None


LLMEvent = TextDelta | Usage


class LLMError(Exception):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        retry_after: float | None = None,
        rate_limited: bool = False,
    ):
        super().__init__(message)
        self.retryable = retryable
        # Seconds the provider asked us to wait (HTTP retry-after); Celery uses it as the countdown.
        self.retry_after = retry_after
        # Rate-limit waits are normal on free tiers and use a separate, larger retry budget.
        self.rate_limited = rate_limited


class LLMProvider(Protocol):
    name: str
    model: str

    def stream(self, request: LLMRequest) -> Iterator[LLMEvent]: ...
