"""Claim-based review cache backed by Postgres.

The UNIQUE constraint on ``review_results.cache_key`` is used as a distributed lock:

1. ``INSERT ... ON CONFLICT DO NOTHING`` — exactly one concurrent caller creates the row and
   becomes the *owner* (the only one allowed to call the LLM). Postgres makes the losers wait
   for the winner's commit, then do nothing.
2. Losers look at the existing row: ``complete`` → cache hit, reuse immediately; ``pending`` /
   ``streaming`` → follow the owner's stream instead of paying for a second LLM call.
3. Owners that crash stop refreshing ``claimed_at``; once the lease expires, or if the result
   is ``failed``, the next caller takes it over with a conditional UPDATE (row-locked, so
   again only one caller wins).

Only successful generations ever reach ``complete``, so errors are never served from cache.
"""

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import ResultStatus, Review, ReviewResult, ReviewStatus


@dataclass(frozen=True)
class Claim:
    result_id: int
    owner: bool  # True → caller must generate the review
    status: str  # result status at claim time
    attempt: int = 1

    @property
    def cache_hit(self) -> bool:
        return not self.owner


@dataclass(frozen=True)
class ResultMeta:
    provider: str
    model: str
    prompt_version: str


def claim(session: Session, cache_key: str, meta: ResultMeta, lease_seconds: int) -> Claim:
    inserted = session.execute(
        insert(ReviewResult)
        .values(
            cache_key=cache_key,
            status=ResultStatus.PENDING,
            provider=meta.provider,
            model=meta.model,
            prompt_version=meta.prompt_version,
            review_text="",
            attempts=1,
        )
        .on_conflict_do_nothing(index_elements=["cache_key"])
        .returning(ReviewResult.id)
    ).scalar_one_or_none()
    if inserted is not None:
        session.commit()
        return Claim(result_id=inserted, owner=True, status=ResultStatus.PENDING)

    stale_before = func.now() - timedelta(seconds=lease_seconds)
    taken_over = session.execute(
        update(ReviewResult)
        .where(
            ReviewResult.cache_key == cache_key,
            or_(
                ReviewResult.status == ResultStatus.FAILED,
                and_(
                    ReviewResult.status.in_([ResultStatus.PENDING, ResultStatus.STREAMING]),
                    ReviewResult.claimed_at < stale_before,
                ),
            ),
        )
        .values(
            status=ResultStatus.PENDING,
            claimed_at=func.now(),
            attempts=ReviewResult.attempts + 1,
            review_text="",
            error=None,
        )
        .returning(ReviewResult.id, ReviewResult.attempts)
    ).one_or_none()
    if taken_over is not None:
        session.commit()
        return Claim(
            result_id=taken_over.id,
            owner=True,
            status=ResultStatus.PENDING,
            attempt=taken_over.attempts,
        )

    existing = session.execute(
        select(ReviewResult.id, ReviewResult.status).where(ReviewResult.cache_key == cache_key)
    ).one()
    session.commit()
    return Claim(result_id=existing.id, owner=False, status=existing.status)


def attach_review(session: Session, review_id: int, claim_: Claim) -> str:
    """Point the audit row at the result, inheriting the result's *current* status.

    The result row is read ``FOR SHARE``: an owner completing/failing the result needs an
    exclusive lock on that row, so the two operations serialize. Without it a follower could read
    "streaming", the owner could then complete and sync all attached reviews, and the follower
    would attach afterwards with the stale "streaming" status forever.
    """
    current = session.execute(
        select(ReviewResult.status)
        .where(ReviewResult.id == claim_.result_id)
        .with_for_update(read=True)
    ).scalar_one()
    status = {
        ResultStatus.COMPLETE: ReviewStatus.COMPLETE,
        ResultStatus.FAILED: ReviewStatus.FAILED,
        ResultStatus.STREAMING: ReviewStatus.STREAMING,
        ResultStatus.PENDING: ReviewStatus.STREAMING if claim_.owner else ReviewStatus.PENDING,
    }[current]
    session.execute(
        update(Review)
        .where(Review.id == review_id)
        .values(result_id=claim_.result_id, cache_hit=claim_.cache_hit, status=status, error=None)
    )
    session.commit()
    return current


def mark_streaming(session: Session, result_id: int) -> None:
    session.execute(
        update(ReviewResult)
        .where(ReviewResult.id == result_id)
        .values(status=ResultStatus.STREAMING, claimed_at=func.now())
    )
    _sync_followers(session, result_id, ReviewStatus.STREAMING)
    session.commit()


def refresh_lease(session: Session, result_id: int) -> None:
    session.execute(
        update(ReviewResult).where(ReviewResult.id == result_id).values(claimed_at=func.now())
    )
    session.commit()


def complete(
    session: Session,
    result_id: int,
    *,
    text: str,
    input_tokens: int | None,
    output_tokens: int | None,
    latency_ms: int,
    context: dict | None,
) -> None:
    session.execute(
        update(ReviewResult)
        .where(ReviewResult.id == result_id)
        .values(
            status=ResultStatus.COMPLETE,
            review_text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            context=context,
            error=None,
            completed_at=func.now(),
        )
    )
    _sync_followers(session, result_id, ReviewStatus.COMPLETE)
    session.commit()


def fail(session: Session, result_id: int, error: str, *, final: bool = True) -> None:
    """Mark the result failed so the next attempt can take it over.

    Attached reviews only become ``failed`` when no retry is coming. During a pending retry they go
    back to ``pending``: a failed review is terminal, and a unit whose reviews are all terminal gets
    published to GitHub, which would post a false failure while the retry is still queued.
    """
    session.execute(
        update(ReviewResult)
        .where(ReviewResult.id == result_id)
        .values(status=ResultStatus.FAILED, error=error[:4000])
    )
    session.execute(
        update(Review)
        .where(Review.result_id == result_id, Review.status.not_in(ReviewStatus.TERMINAL))
        .values(
            status=ReviewStatus.FAILED if final else ReviewStatus.PENDING,
            error=error[:4000],
        )
    )
    session.commit()


def _sync_followers(session: Session, result_id: int, status: str) -> None:
    """Every audit row attached to this result (owner and followers) moves together."""
    session.execute(
        update(Review)
        .where(Review.result_id == result_id, Review.status != ReviewStatus.SKIPPED)
        .values(status=status, error=None)
    )
