"""Review of a single file: the orchestration behind the ``review_file`` Celery task.

Kept free of Celery so it can be tested directly with injected dependencies.

    fetch content → cache key → claim ──(hit)──▶ attach audit row, done (no LLM call)
                                      ├(follow)▶ attach audit row, share the owner's stream
                                      └(owner)─▶ tree-sitter → RAG → prompt → LLM stream
                                                 → Redis Stream → complete/fail result
"""

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import redis
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.cache import service as cache
from app.cache.keys import build_cache_key, content_hash
from app.config import Settings
from app.db.models import Commit, IndexStatus, Repo, ResultStatus, Review, ReviewStatus
from app.llm.adapter import LLMError, LLMProvider, LLMRequest, TextDelta, Usage
from app.parsing.languages import detect_language
from app.parsing.treesitter import extract_changed_chunks
from app.rag.chroma_store import CodeIndex
from app.rag.retriever import Retriever
from app.review.prompt import PROMPT_VERSION, PromptBudget, build_prompt
from app.sources.base import SourceError, SourceProvider
from app.streaming.publisher import StreamPublisher

if TYPE_CHECKING:
    from app.github.client import GitHubClient

logger = logging.getLogger(__name__)

LEASE_REFRESH_SECONDS = 30


@dataclass
class PipelineDeps:
    sessionmaker: sessionmaker[Session]
    source: SourceProvider
    llm: LLMProvider
    redis: redis.Redis
    settings: Settings
    index: CodeIndex | None = None
    retriever: Retriever | None = None
    github: "GitHubClient | None" = None  # set in GitHub source mode; also used to post results


class Outcome:
    MISSING = "missing"
    ALREADY_DONE = "already_done"
    SKIPPED = "skipped"
    CACHE_HIT = "cache_hit"
    FOLLOWING = "following"
    GENERATED = "generated"


def _skip(session: Session, review_id: int, reason: str) -> str:
    session.execute(
        update(Review)
        .where(Review.id == review_id)
        .values(status=ReviewStatus.SKIPPED, skip_reason=reason)
    )
    session.commit()
    return Outcome.SKIPPED


def mark_review_failed(session_factory: sessionmaker[Session], review_id: int, error: str) -> None:
    """For failures before a result was attached (e.g. GitHub unreachable after all retries)."""
    with session_factory() as session:
        session.execute(
            update(Review)
            .where(Review.id == review_id, Review.status.not_in(ReviewStatus.TERMINAL))
            .values(status=ReviewStatus.FAILED, error=error[:4000])
        )
        session.commit()


def will_retry(exc: BaseException, retries_used: int | None, settings: Settings) -> bool:
    """Single retry rule shared by the pipeline (what to tell the browser) and the Celery task.

    ``retries_used=None`` means the caller cannot retry. Rate-limit waits (HTTP 429 on free tiers)
    are expected and get their own larger budget, so they don't burn the error-retry budget.
    """
    if retries_used is None or not getattr(exc, "retryable", False):
        return False
    limit = (
        settings.rate_limit_max_retries
        if getattr(exc, "rate_limited", False)
        else settings.task_max_retries
    )
    return retries_used < limit


def review_file(review_id: int, deps: PipelineDeps, *, retries_used: int | None = None) -> str:
    settings = deps.settings
    with deps.sessionmaker() as session:
        row = session.execute(
            select(Review, Commit, Repo)
            .join(Commit, Review.commit_id == Commit.id)
            .join(Repo, Review.repo_id == Repo.id)
            .where(Review.id == review_id)
        ).one_or_none()
        if row is None:
            return Outcome.MISSING
        review, commit, repo = row
        if review.status in (ReviewStatus.COMPLETE, ReviewStatus.SKIPPED):
            return Outcome.ALREADY_DONE

        patch = review.patch or ""
        if len(patch) > settings.max_patch_chars:
            return _skip(
                session, review.id, f"diff larger than {settings.max_patch_chars} characters"
            )
        content = deps.source.get_file_content(
            repo.full_name, review.file_path, commit.sha, settings.max_file_bytes
        )
        if content is None:
            return _skip(session, review.id, "file is binary, missing at this commit, or too large")

        language = detect_language(review.file_path)
        review.content_hash = content_hash(content, patch)
        session.commit()

        key = build_cache_key(
            repo_id=repo.id,
            provider=deps.llm.name,
            model=deps.llm.model,
            prompt_version=PROMPT_VERSION,
            language=language,
            file_content=content,
            patch=patch,
        )
        claim = cache.claim(
            session,
            key,
            cache.ResultMeta(
                provider=deps.llm.name, model=deps.llm.model, prompt_version=PROMPT_VERSION
            ),
            settings.cache_lease_seconds,
        )
        result_status = cache.attach_review(session, review.id, claim)

        if not claim.owner:
            if result_status == ResultStatus.FAILED:
                # Lost a race with a failure that happened between our takeover check and now.
                raise LLMError("shared result failed; retrying", retryable=True)
            logger.info(
                "review %s: cache %s (result %s)", review.id, result_status, claim.result_id
            )
            return (
                Outcome.CACHE_HIT if result_status == ResultStatus.COMPLETE else Outcome.FOLLOWING
            )

        _generate(
            session,
            deps,
            claim=claim,
            repo=repo,
            path=review.file_path,
            language=language,
            content=content,
            patch=patch,
            commit_message=commit.message,
            retries_used=retries_used,
        )
        return Outcome.GENERATED


def _generate(
    session: Session,
    deps: PipelineDeps,
    *,
    claim: cache.Claim,
    repo: Repo,
    path: str,
    language: str | None,
    content: str,
    patch: str,
    commit_message: str,
    retries_used: int | None,
) -> None:
    settings = deps.settings
    publisher = StreamPublisher(
        deps.redis,
        claim.result_id,
        flush_interval_ms=settings.stream_flush_interval_ms,
        ttl_seconds=settings.stream_ttl_seconds,
    )
    started = time.monotonic()
    try:
        publisher.start(claim.attempt)
        cache.mark_streaming(session, claim.result_id)

        chunks = extract_changed_chunks(path, content, patch)
        similar = []
        if deps.retriever is not None and repo.index_status == IndexStatus.READY:
            similar = deps.retriever.similar_code(repo.id, path, chunks)
        prompt = build_prompt(
            path=path,
            language=language,
            commit_message=commit_message,
            patch=patch,
            changed_chunks=chunks,
            similar=similar,
            budget=PromptBudget(settings.effective_max_prompt_chars),
        )

        parts: list[str] = []
        usage = Usage(None, None)
        last_lease = time.monotonic()
        request = LLMRequest(
            system=prompt.system, user=prompt.user, max_tokens=settings.effective_max_output_tokens
        )
        for event in deps.llm.stream(request):
            if isinstance(event, TextDelta):
                parts.append(event.text)
                publisher.write(event.text)
                if time.monotonic() - last_lease > LEASE_REFRESH_SECONDS:
                    cache.refresh_lease(
                        session, claim.result_id
                    )  # still alive; don't let others take over
                    last_lease = time.monotonic()
            else:
                usage = event
        text = "".join(parts)
        if not text.strip():
            raise LLMError("model returned an empty review", retryable=True)

        latency_ms = int((time.monotonic() - started) * 1000)
        context: dict[str, Any] = {**prompt.context, "rag_enabled": deps.retriever is not None}
        # Persist before announcing "done": a client that reacts to "done" by refetching sees the result.
        cache.complete(
            session,
            claim.result_id,
            text=text,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            latency_ms=latency_ms,
            context=context,
        )
        publisher.done(latency_ms=latency_ms, output_tokens=usage.output_tokens, cache_hit=False)
    except Exception as exc:
        session.rollback()
        retrying = will_retry(exc, retries_used, settings)
        message = str(exc) or exc.__class__.__name__
        logger.warning("result %s failed (retrying=%s): %s", claim.result_id, retrying, message)
        cache.fail(session, claim.result_id, message)
        try:
            publisher.failed(message, retrying=retrying)
        except redis.RedisError:
            logger.warning("could not publish failure for result %s", claim.result_id)
        raise


__all__ = [
    "LLMError",
    "Outcome",
    "PipelineDeps",
    "SourceError",
    "mark_review_failed",
    "review_file",
]
