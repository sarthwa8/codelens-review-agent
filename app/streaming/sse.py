"""SSE endpoint: bridges a review's Redis Stream to the browser.

Delivery rules:
* Finished review (complete / failed / skipped) → answer straight from Postgres. Works forever,
  long after the Redis stream has expired.
* In progress → replay the Redis Stream from ``Last-Event-ID`` (or the start), then tail it with
  ``XREAD BLOCK``. Each SSE event's ``id`` is the stream entry id, so EventSource reconnects resume
  exactly where they left off.
* Idle periods → comment heartbeats so proxies don't drop the connection, plus a DB re-check in
  case the stream was lost (e.g. Redis restart) while the result completed.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.auth.deps import get_viewer
from app.auth.sessions import Viewer
from app.config import Settings, get_settings
from app.db.models import Repo, ResultStatus, Review, ReviewResult, ReviewStatus
from app.db.session import get_sessionmaker
from app.redis_client import get_async_redis
from app.streaming.publisher import stream_key

logger = logging.getLogger(__name__)
router = APIRouter(tags=["streaming"])

QUEUED_POLL_SECONDS = 1.0


@dataclass(frozen=True)
class ReviewState:
    status: str
    result_id: int | None
    result_status: str | None
    text: str
    cache_hit: bool
    skip_reason: str | None
    error: str | None
    latency_ms: int | None
    output_tokens: int | None
    repo_github_id: int | None = None


def load_review_state(review_id: int) -> ReviewState | None:
    with get_sessionmaker()() as session:
        row = session.execute(
            select(Review, ReviewResult, Repo.github_id)
            .join(Repo, Review.repo_id == Repo.id)
            .outerjoin(ReviewResult, Review.result_id == ReviewResult.id)
            .where(Review.id == review_id)
        ).one_or_none()
    if row is None:
        return None
    review, result, repo_github_id = row
    return ReviewState(
        status=review.status,
        result_id=review.result_id,
        result_status=result.status if result else None,
        text=result.review_text if result else "",
        cache_hit=review.cache_hit,
        skip_reason=review.skip_reason,
        error=review.error or (result.error if result else None),
        latency_ms=result.latency_ms if result else None,
        output_tokens=result.output_tokens if result else None,
        repo_github_id=repo_github_id,
    )


def format_sse(event: str, data: dict[str, Any] | str, event_id: str | None = None) -> str:
    payload = data if isinstance(data, str) else json.dumps(data)
    prefix = f"id: {event_id}\n" if event_id else ""
    return f"{prefix}event: {event}\ndata: {payload}\n\n"


def _terminal_events(state: ReviewState) -> list[str] | None:
    """Events that fully describe a finished review, or None if it's still in progress."""
    if state.status == ReviewStatus.SKIPPED:
        return [
            format_sse("skipped", {"reason": state.skip_reason}),
            format_sse("done", {"status": "skipped"}),
        ]
    if state.result_status == ResultStatus.COMPLETE:
        return [
            format_sse("snapshot", {"text": state.text, "cache_hit": state.cache_hit}),
            format_sse(
                "done",
                {"status": "complete", "cache_hit": state.cache_hit, "latency_ms": state.latency_ms,
                 "output_tokens": state.output_tokens},
            ),
        ]  # fmt: skip
    if state.status == ReviewStatus.FAILED and state.result_status in (None, ResultStatus.FAILED):
        return [format_sse("failed", {"message": state.error or "review failed"})]
    return None


async def review_events(
    review_id: int,
    request: Request,
    redis: aioredis.Redis,
    settings: Settings,
    last_event_id: str | None,
    loader: Callable[[int], ReviewState | None] = load_review_state,
) -> AsyncIterator[str]:
    yield "retry: 3000\n\n"
    state = await run_in_threadpool(loader, review_id)
    announced_queue = False
    idle = 0.0

    # Phase 1: the review may still be waiting in the Celery queue with no result attached.
    while state is not None and state.result_id is None:
        if (events := _terminal_events(state)) is not None:
            for event in events:
                yield event
            return
        if not announced_queue:
            yield format_sse("status", {"status": "queued"})
            announced_queue = True
        await asyncio.sleep(QUEUED_POLL_SECONDS)
        idle += QUEUED_POLL_SECONDS
        if idle >= settings.sse_heartbeat_seconds:
            yield ": heartbeat\n\n"
            idle = 0.0
        if await request.is_disconnected():
            return
        state = await run_in_threadpool(loader, review_id)
    if state is None or state.result_id is None:
        return

    if (events := _terminal_events(state)) is not None:
        for event in events:
            yield event
        return

    # Phase 2: replay + tail the result's stream (shared by every review attached to that result).
    key = stream_key(state.result_id)
    cursor = last_event_id or "0-0"
    if state.cache_hit:
        yield format_sse("status", {"status": "following", "cache_hit": True})
    while True:
        if await request.is_disconnected():
            return
        response = await redis.xread(
            {key: cursor}, block=settings.sse_heartbeat_seconds * 1000, count=500
        )
        if not response:
            state = await run_in_threadpool(loader, review_id)
            if state is not None and (events := _terminal_events(state)) is not None:
                for event in events:
                    yield event
                return
            yield ": heartbeat\n\n"
            continue
        for _key, entries in response:
            for entry_id, fields in entries:
                cursor = entry_id
                event_type = fields.get("type", "delta")
                yield format_sse(event_type, fields.get("data", "{}"), event_id=entry_id)
                if event_type in ("done", "failed"):
                    return


@router.get("/reviews/{review_id}/stream")
async def stream_review(
    review_id: int,
    request: Request,
    last_event_id: str | None = Header(default=None),
    redis: aioredis.Redis = Depends(get_async_redis),
    settings: Settings = Depends(get_settings),
    viewer: Viewer = Depends(get_viewer),
) -> StreamingResponse:
    state = await run_in_threadpool(load_review_state, review_id)
    if state is None or state.repo_github_id is None or not viewer.can_see(state.repo_github_id):
        raise HTTPException(status_code=404, detail="review not found")
    return StreamingResponse(
        review_events(review_id, request, redis, settings, last_event_id),
        media_type="text/event-stream",
        # X-Accel-Buffering: nginx otherwise buffers the response and delivers it all at the end.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
