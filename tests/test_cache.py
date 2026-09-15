"""The content-hash cache is CodeLens' core cost-saving mechanism. These tests pin down:

* same content  → no second LLM call, but a second audit row
* anything that should change the review (model, prompt, repo) → cache miss
* concurrency   → N workers racing on the same content make exactly one LLM call
* failures      → never cached; the next attempt regenerates
* crashed owner → lease expires and another worker takes over
"""

import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.cache import service as cache
from app.cache.keys import build_cache_key
from app.db.models import Commit, ResultStatus, Review, ReviewResult, ReviewStatus
from app.llm.adapter import LLMError, LLMEvent, LLMRequest, TextDelta, Usage
from app.llm.fake_provider import FakeProvider
from app.review import pipeline
from app.review.pipeline import Outcome
from tests.helpers import load_review, result_count, seed_reviews

ORDERS_V1 = "def total(items):\n    return sum(i.price for i in items)\n"
ORDERS_V2 = "def total(items):\n    return sum(i.price * i.qty for i in items)\n"


class FlakyLLM:
    """Fails the first ``failures`` calls, then streams a fixed review."""

    name = "flaky"
    model = "flaky-1"

    def __init__(self, failures: int, retryable: bool = True):
        self.failures = failures
        self.retryable = retryable
        self.calls = 0

    def stream(self, request: LLMRequest) -> Iterator[LLMEvent]:
        self.calls += 1
        yield TextDelta("partial output that must never be cached ")
        if self.calls <= self.failures:
            raise LLMError("upstream 529 overloaded", retryable=self.retryable)
        yield TextDelta("### Summary\nfine")
        yield Usage(10, 5)


def make_push(
    git_repo,
    deps,
    ref: str,
    files: dict[str, str],
    github_id: int = 1,
    full_name: str | None = None,
):
    sha = git_repo.commit(files, message=f"push to {ref}")
    return seed_reviews(
        deps.sessionmaker,
        deps.source,
        full_name=full_name or git_repo.full_name,
        sha=sha,
        ref=ref,
        github_id=github_id,
    )


# --- cache key -------------------------------------------------------------------------------------

BASE_KEY = dict(
    repo_id=1,
    provider="anthropic",
    model="m",
    prompt_version="v1",
    language="python",
    file_content="abc",
    patch="+x",
)


def test_cache_key_is_deterministic_sha256() -> None:
    key = build_cache_key(**BASE_KEY)
    assert key == build_cache_key(**BASE_KEY)
    assert len(key) == 64 and int(key, 16) >= 0


@pytest.mark.parametrize(
    "field", ["repo_id", "provider", "model", "prompt_version", "language", "file_content", "patch"]
)
def test_every_key_field_changes_the_key(field: str) -> None:
    changed = {**BASE_KEY, field: 2 if field == "repo_id" else "different"}
    assert build_cache_key(**changed) != build_cache_key(**BASE_KEY)


def test_cache_key_fields_are_length_prefixed() -> None:
    """Naive concatenation would make ("ab", "c") and ("a", "bc") collide."""
    a = build_cache_key(**{**BASE_KEY, "file_content": "ab", "patch": "c"})
    b = build_cache_key(**{**BASE_KEY, "file_content": "a", "patch": "bc"})
    assert a != b


# --- pipeline cache behaviour -----------------------------------------------------------------------


def test_same_content_skips_llm_and_still_records_audit_row(git_repo, deps) -> None:
    [first] = make_push(git_repo, deps, "refs/heads/feature", {"orders.py": ORDERS_V1})
    assert pipeline.review_file(first, deps) == Outcome.GENERATED
    assert FakeProvider.calls == 1

    # The same commit content arriving again (e.g. fast-forward merge into main).
    with deps.sessionmaker() as session:
        original = session.get(Review, first)
        commit = session.get(Commit, original.commit_id)
        second_commit = Commit(
            repo_id=commit.repo_id, sha=commit.sha, ref="refs/heads/main", message="merge"
        )
        session.add(second_commit)
        session.flush()
        duplicate = Review(
            repo_id=commit.repo_id,
            commit_id=second_commit.id,
            file_path="orders.py",
            patch=original.patch,
        )
        session.add(duplicate)
        session.commit()
        second = duplicate.id

    started = time.perf_counter()
    assert pipeline.review_file(second, deps) == Outcome.CACHE_HIT
    elapsed = time.perf_counter() - started

    assert FakeProvider.calls == 1, "cache hit must not call the LLM"
    assert elapsed < 1.0, f"cache hit path should be near-instant, took {elapsed:.3f}s"
    review_a, result_a = load_review(deps.sessionmaker, first)
    review_b, result_b = load_review(deps.sessionmaker, second)
    assert review_a.id != review_b.id, "each push gets its own audit row"
    assert result_a.id == result_b.id
    assert (review_a.cache_hit, review_b.cache_hit) == (False, True)
    assert review_b.status == ReviewStatus.COMPLETE
    assert review_a.content_hash == review_b.content_hash
    assert result_count(deps.sessionmaker) == 1


def test_reapplied_change_on_a_new_commit_is_a_cache_hit(git_repo, deps) -> None:
    """Revert then re-apply: new SHAs, identical content + patch → served from cache."""
    make_push(git_repo, deps, "refs/heads/main", {"orders.py": ORDERS_V1})
    [change] = make_push(git_repo, deps, "refs/heads/main", {"orders.py": ORDERS_V2})
    [revert] = make_push(git_repo, deps, "refs/heads/main", {"orders.py": ORDERS_V1})
    [reapply] = make_push(git_repo, deps, "refs/heads/main", {"orders.py": ORDERS_V2})

    assert pipeline.review_file(change, deps) == Outcome.GENERATED
    assert pipeline.review_file(revert, deps) == Outcome.GENERATED
    assert pipeline.review_file(reapply, deps) == Outcome.CACHE_HIT
    assert FakeProvider.calls == 2


def test_different_model_is_a_cache_miss(git_repo, deps) -> None:
    [a] = make_push(git_repo, deps, "refs/heads/a", {"orders.py": ORDERS_V1})
    pipeline.review_file(a, deps)

    other_model = replace(deps, llm=FakeProvider(model="fake-reviewer-2", delay_ms=0))
    with deps.sessionmaker() as session:
        original = session.get(Review, a)
        commit = Commit(
            repo_id=original.repo_id, sha="f" * 40, ref="refs/heads/b", message="same code"
        )
        session.add(commit)
        session.flush()
        again = Review(
            repo_id=original.repo_id,
            commit_id=commit.id,
            file_path="orders.py",
            patch=original.patch,
        )
        session.add(again)
        session.commit()
        again_id = again.id

    # The content lives at the original SHA; point the source at it for this synthetic commit.
    other_model = replace(
        other_model, source=_AliasSource(deps.source, {"f" * 40: git_repo.shas[0]})
    )
    assert pipeline.review_file(again_id, other_model) == Outcome.GENERATED
    assert FakeProvider.calls == 2
    assert result_count(deps.sessionmaker) == 2


def test_different_prompt_version_is_a_cache_miss(git_repo, deps, monkeypatch) -> None:
    [a] = make_push(git_repo, deps, "refs/heads/a", {"orders.py": ORDERS_V1})
    pipeline.review_file(a, deps)
    monkeypatch.setattr(pipeline, "PROMPT_VERSION", "next-version")
    with deps.sessionmaker() as session:
        session.execute(update(ReviewResult).values(status=ResultStatus.COMPLETE))
        session.execute(update(Review).where(Review.id == a).values(status=ReviewStatus.PENDING))
        session.commit()
    assert pipeline.review_file(a, deps) == Outcome.GENERATED
    assert FakeProvider.calls == 2


def test_identical_content_in_another_repo_is_not_shared(git_repo, deps) -> None:
    """Results may quote same-repo context, so they must never leak across repositories."""
    sha = git_repo.commit({"orders.py": ORDERS_V1})
    [a] = seed_reviews(
        deps.sessionmaker, deps.source, full_name=git_repo.full_name, sha=sha, github_id=1
    )
    fork = type(git_repo)(root=git_repo.root, full_name="someone/shop-fork").init()
    fork_sha = fork.commit({"orders.py": ORDERS_V1})
    [b] = seed_reviews(
        deps.sessionmaker, deps.source, full_name=fork.full_name, sha=fork_sha, github_id=2
    )

    assert pipeline.review_file(a, deps) == Outcome.GENERATED
    assert pipeline.review_file(b, deps) == Outcome.GENERATED
    assert FakeProvider.calls == 2


def test_concurrent_duplicates_make_exactly_one_llm_call(git_repo, deps) -> None:
    sha = git_repo.commit({"orders.py": ORDERS_V1})
    workers = 8
    review_ids = []
    for i in range(workers):
        review_ids += seed_reviews(
            deps.sessionmaker,
            deps.source,
            full_name=git_repo.full_name,
            sha=sha,
            ref=f"refs/heads/b{i}",
        )

    slow = replace(
        deps, llm=FakeProvider(delay_ms=2)
    )  # keep the owner streaming while others arrive
    barrier = threading.Barrier(workers)
    outcomes: list[str] = []
    errors: list[BaseException] = []

    def run(review_id: int) -> None:
        barrier.wait()
        try:
            outcomes.append(pipeline.review_file(review_id, slow))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(rid,)) for rid in review_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert FakeProvider.calls == 1
    assert outcomes.count(Outcome.GENERATED) == 1
    assert set(outcomes) <= {Outcome.GENERATED, Outcome.FOLLOWING, Outcome.CACHE_HIT}
    rows = [load_review(deps.sessionmaker, rid) for rid in review_ids]
    assert len({result.id for _, result in rows}) == 1
    # Followers that attached mid-stream must end up complete too (FOR SHARE lock regression test).
    assert all(review.status == ReviewStatus.COMPLETE for review, _ in rows)
    assert sum(review.cache_hit for review, _ in rows) == workers - 1


def test_failed_generation_is_never_cached(git_repo, deps) -> None:
    [review_id] = make_push(git_repo, deps, "refs/heads/main", {"orders.py": ORDERS_V1})
    flaky = FlakyLLM(failures=1)
    flaky_deps = replace(deps, llm=flaky)

    with pytest.raises(LLMError):
        pipeline.review_file(review_id, flaky_deps, retries_used=0)
    review, result = load_review(deps.sessionmaker, review_id)
    assert result.status == ResultStatus.FAILED
    # A retry is coming, so the review isn't terminal (that would publish a false failure).
    assert review.status == ReviewStatus.PENDING
    assert "partial output" not in result.review_text  # partial text was never persisted

    # Retry: the failed result is taken over and regenerated rather than served.
    assert pipeline.review_file(review_id, flaky_deps) == Outcome.GENERATED
    review, result = load_review(deps.sessionmaker, review_id)
    assert flaky.calls == 2
    assert result.status == ResultStatus.COMPLETE
    assert result.attempts == 2
    assert review.status == ReviewStatus.COMPLETE and not review.cache_hit

    # Stream: the retry started with a reset so clients discard the failed attempt's partial text.
    entries = deps.redis.xrange(f"codelens:stream:{result.id}")
    assert entries[0][1]["type"] == "reset"
    assert entries[-1][1]["type"] == "done"
    assert not any(fields["type"] == "retrying" for _, fields in entries)


def test_expired_lease_is_taken_over(deps) -> None:
    meta = cache.ResultMeta(provider="fake", model="fake-reviewer-1", prompt_version="v")
    with deps.sessionmaker() as session:
        crashed = cache.claim(session, "k" * 64, meta, lease_seconds=60)
        assert crashed.owner
        # Still leased → a second caller must follow, not take over.
        assert not cache.claim(session, "k" * 64, meta, lease_seconds=60).owner
        session.execute(
            update(ReviewResult)
            .where(ReviewResult.id == crashed.result_id)
            .values(claimed_at=ReviewResult.claimed_at - timedelta(minutes=10))
        )
        session.commit()
        takeover = cache.claim(session, "k" * 64, meta, lease_seconds=60)
    assert takeover.owner and takeover.result_id == crashed.result_id and takeover.attempt == 2


def test_completed_review_is_idempotent_on_redelivery(git_repo, deps) -> None:
    [review_id] = make_push(git_repo, deps, "refs/heads/main", {"orders.py": ORDERS_V1})
    assert pipeline.review_file(review_id, deps) == Outcome.GENERATED
    assert pipeline.review_file(review_id, deps) == Outcome.ALREADY_DONE
    assert FakeProvider.calls == 1


class _AliasSource:
    def __init__(self, inner, aliases: dict[str, str]):
        self.inner = inner
        self.aliases = aliases

    def get_commit_files(self, full_name, sha):
        return self.inner.get_commit_files(full_name, self.aliases.get(sha, sha))

    def get_file_content(self, full_name, path, ref, max_bytes):
        return self.inner.get_file_content(full_name, path, self.aliases.get(ref, ref), max_bytes)

    def iter_repo_files(self, full_name, ref, max_bytes):
        return self.inner.iter_repo_files(full_name, self.aliases.get(ref, ref), max_bytes)


# --- repository upsert (found by the webhook load test) ----------------------------------------


def test_concurrent_first_pushes_for_a_new_repo_do_not_crash(db) -> None:
    from app.tasks.review_tasks import upsert_repo

    info = {"github_id": 777, "full_name": "race/repo", "default_branch": "main"}
    workers = 12
    barrier = threading.Barrier(workers)
    ids: list[int] = []
    errors: list[BaseException] = []

    def run() -> None:
        with db() as session:
            session.execute(select(1))  # check out a live connection first so inserts truly overlap
            session.commit()
            barrier.wait()
            try:
                ids.append(upsert_repo(session, info))
            except BaseException as exc:
                errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    assert len(set(ids)) == 1


def test_new_repo_can_take_the_name_of_a_renamed_one(db) -> None:
    from app.db.models import Repo
    from app.tasks.review_tasks import upsert_repo

    with db() as session:
        old = upsert_repo(
            session, {"github_id": 1, "full_name": "acme/api", "default_branch": "main"}
        )
        new = upsert_repo(
            session, {"github_id": 2, "full_name": "acme/api", "default_branch": "main"}
        )
        assert old != new
        assert session.get(Repo, new).full_name == "acme/api"
        assert session.get(Repo, old).full_name == f"acme/api#renamed-{old}"


def test_repo_upsert_retries_after_a_non_arbiter_unique_violation(db) -> None:
    """Deterministic version of the race: the first INSERT hits the full_name unique index."""
    from sqlalchemy import event
    from sqlalchemy.exc import IntegrityError

    from app.db.session import get_engine
    from app.tasks.review_tasks import upsert_repo

    engine = get_engine()
    raised = []

    def fail_first_repo_insert(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO REPOS") and not raised:
            raised.append(statement)
            raise IntegrityError(
                statement, parameters, Exception('duplicate key "repos_full_name_key"')
            )

    event.listen(engine, "before_cursor_execute", fail_first_repo_insert)
    try:
        with db() as session:
            repo_id = upsert_repo(
                session, {"github_id": 5, "full_name": "retry/me", "default_branch": "main"}
            )
    finally:
        event.remove(engine, "before_cursor_execute", fail_first_repo_insert)
    assert raised, "the injected failure never fired"
    assert repo_id > 0


def test_final_generation_failure_marks_reviews_failed(git_repo, deps) -> None:
    [review_id] = make_push(git_repo, deps, "refs/heads/main", {"orders.py": ORDERS_V1})
    with pytest.raises(LLMError):
        pipeline.review_file(review_id, replace(deps, llm=FlakyLLM(failures=5)), retries_used=None)
    review, result = load_review(deps.sessionmaker, review_id)
    assert (review.status, result.status) == (ReviewStatus.FAILED, ResultStatus.FAILED)
