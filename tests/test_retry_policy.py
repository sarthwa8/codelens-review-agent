from collections.abc import Iterator
from dataclasses import replace

import pytest

from app.config import Settings
from app.llm.adapter import LLMError, LLMEvent, LLMRequest, TextDelta, Usage
from app.review import pipeline
from app.sources.base import SourceError
from tests.helpers import seed_reviews

SETTINGS = Settings(_env_file=None, task_max_retries=3, rate_limit_max_retries=30)


@pytest.mark.parametrize(
    ("exc", "retries_used", "expected"),
    [
        (LLMError("503", retryable=True), 0, True),
        (LLMError("503", retryable=True), 3, False),  # error budget spent
        (
            LLMError("429", retryable=True, rate_limited=True),
            3,
            True,
        ),  # rate limits have their own budget
        (LLMError("429", retryable=True, rate_limited=True), 30, False),
        (LLMError("413", retryable=False), 0, False),
        (SourceError("GitHub rate limit", retryable=True, rate_limited=True), 10, True),
        (LLMError("503", retryable=True), None, False),  # caller cannot retry
        (ValueError("bug"), 0, False),
    ],
)
def test_will_retry(exc: BaseException, retries_used: int | None, expected: bool) -> None:
    assert pipeline.will_retry(exc, retries_used, SETTINGS) is expected


class CapturingLLM:
    name = "groq"
    model = "openai/gpt-oss-120b"

    def __init__(self, fail_with: LLMError | None = None):
        self.requests: list[LLMRequest] = []
        self.fail_with = fail_with

    def stream(self, request: LLMRequest) -> Iterator[LLMEvent]:
        self.requests.append(request)
        if self.fail_with:
            raise self.fail_with
        yield TextDelta("### Summary\nok")
        yield Usage(1, 1)


def test_groq_budgets_are_applied_to_prompt_and_output(git_repo, deps) -> None:
    big = "\n".join(f"def f{i}(x):\n    return x + {i}\n" for i in range(600))
    sha = git_repo.commit({"big.py": big})
    [review_id] = seed_reviews(
        deps.sessionmaker, deps.source, full_name=git_repo.full_name, sha=sha
    )
    llm = CapturingLLM()
    groq_deps = replace(
        deps, llm=llm, settings=deps.settings.model_copy(update={"llm_provider": "groq"})
    )

    assert pipeline.review_file(review_id, groq_deps) == pipeline.Outcome.GENERATED
    [request] = llm.requests
    assert request.max_tokens == 1_800
    assert len(request.user) <= 12_000 + 200  # section headers may add a little beyond the budget


def test_rate_limited_failure_tells_clients_it_is_retrying_even_after_error_budget(
    git_repo, deps
) -> None:
    sha = git_repo.commit({"a.py": "def a():\n    return 1\n"})
    [review_id] = seed_reviews(
        deps.sessionmaker, deps.source, full_name=git_repo.full_name, sha=sha
    )
    llm = CapturingLLM(fail_with=LLMError("429", retryable=True, rate_limited=True, retry_after=2))

    with pytest.raises(LLMError):
        pipeline.review_file(
            review_id, replace(deps, llm=llm), retries_used=5
        )  # > task_max_retries
    events = [
        fields["type"]
        for _, fields in deps.redis.xrange(next(iter(deps.redis.scan_iter("codelens:stream:*"))))
    ]
    assert events[-1] == "retrying"
