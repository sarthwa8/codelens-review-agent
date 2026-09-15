"""GitHub webhook receiver.

The handler's only jobs are: authenticate, dedupe, enqueue, return 202. Everything slow
(GitHub API calls, parsing, retrieval, LLM) happens in the Celery worker, so response time
is independent of commit size and LLM latency.
"""

import hashlib
import hmac
import json
import logging
from collections.abc import Callable
from typing import Any

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings
from app.redis_client import get_async_redis

logger = logging.getLogger(__name__)
router = APIRouter(tags=["webhooks"])

ZERO_SHA = "0" * 40
Enqueuer = Callable[[str, dict[str, Any]], None]


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Validate ``X-Hub-Signature-256`` against the *raw* request body.

    The HMAC must be computed over the exact bytes GitHub sent — re-serialising parsed JSON
    changes whitespace/key order and breaks verification. ``compare_digest`` keeps the
    comparison constant-time so the signature can't be recovered byte-by-byte via timing.
    """
    if not secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.removeprefix("sha256="))


REVIEWABLE_PR_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}


class Ignored(Exception):
    """The delivery is valid but needs no work; the reason is returned to GitHub."""


def _repo_info(payload: dict[str, Any]) -> dict[str, Any]:
    repo = payload["repository"]
    return {
        "github_id": int(repo["id"]),
        "full_name": repo["full_name"],
        "default_branch": repo.get("default_branch") or repo.get("master_branch") or "main",
    }


def _installation_id(payload: dict[str, Any]) -> int | None:
    installation = payload.get("installation") or {}
    return int(installation["id"]) if installation.get("id") else None


def build_push_event(payload: dict[str, Any], delivery_id: str | None) -> dict[str, Any]:
    """Reduce a GitHub push payload (up to 25 MB) to the small message the worker needs."""
    raw_commits = payload.get("commits") or []
    if not raw_commits and payload.get("head_commit"):
        # e.g. pushing an existing commit to a new branch: commits[] is empty.
        raw_commits = [payload["head_commit"]]
    commits = []
    for c in raw_commits:
        author = c.get("author") or {}
        commits.append(
            {
                "sha": c["id"],
                "message": (c.get("message") or "")[:4000],
                "author": author.get("username") or author.get("name"),
                "timestamp": c.get("timestamp"),
            }
        )
    return {
        "delivery_id": delivery_id,
        "installation_id": _installation_id(payload),
        "repo": _repo_info(payload),
        "ref": payload["ref"],
        "before": payload.get("before"),
        "after": payload.get("after"),
        "forced": bool(payload.get("forced")),
        "commits": commits,
    }


def build_pull_request_event(payload: dict[str, Any], delivery_id: str | None) -> dict[str, Any]:
    pr = payload["pull_request"]
    return {
        "delivery_id": delivery_id,
        "installation_id": _installation_id(payload),
        "repo": _repo_info(payload),
        "action": payload["action"],
        "number": int(pr["number"]),
        "title": (pr.get("title") or "")[:4000],
        "author": (pr.get("user") or {}).get("login"),
        "head_sha": pr["head"]["sha"],
        "head_ref": pr["head"]["ref"],
        "head_repo_id": ((pr["head"].get("repo") or {}).get("id")),
        "base_sha": pr["base"]["sha"],
        "base_ref": pr["base"]["ref"],
    }


def prepare_task(
    event_name: str | None, payload: dict[str, Any], delivery_id: str | None
) -> tuple[str, dict[str, Any]]:
    """Map a verified delivery to (Celery task name, trimmed event), or raise Ignored."""
    if event_name == "push":
        if payload.get("deleted") or payload.get("after") == ZERO_SHA:
            raise Ignored("branch deletion")
        if not str(payload.get("ref", "")).startswith("refs/heads/"):
            raise Ignored("not a branch push")
        event = build_push_event(payload, delivery_id)
        if not event["commits"]:
            raise Ignored("push contains no commits")
        return "codelens.process_push", event
    if event_name == "pull_request":
        action = payload.get("action")
        if action not in REVIEWABLE_PR_ACTIONS:
            raise Ignored(f"pull_request action '{action}' does not add code to review")
        pr = payload["pull_request"]
        if pr.get("state") != "open":
            raise Ignored("pull request is not open")
        if pr.get("draft"):
            # Drafts are reviewed once marked ready (ready_for_review), which saves LLM quota.
            raise Ignored("draft pull request")
        return "codelens.process_pull_request", build_pull_request_event(payload, delivery_id)
    raise Ignored(f"event '{event_name}' is not handled")


def get_enqueuer() -> Enqueuer:
    # send_task by name: the API process never imports worker code (tree-sitter, chromadb, LLM SDKs).
    from app.celery_app import celery_app

    def enqueue(task_name: str, event: dict[str, Any]) -> None:
        celery_app.send_task(task_name, kwargs={"event": event}, queue="review")

    return enqueue


def _ignored(reason: str) -> dict[str, str]:
    return {"status": "ignored", "reason": reason}


@router.post("/webhooks/github", status_code=202)
async def github_webhook(
    request: Request,
    x_github_event: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
    x_hub_signature_256: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
    redis: aioredis.Redis = Depends(get_async_redis),
    enqueue: Enqueuer = Depends(get_enqueuer),
) -> Any:
    body = await request.body()
    if not verify_signature(settings.github_webhook_secret, body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="invalid signature")

    if x_github_event == "ping":
        return JSONResponse({"status": "pong"}, status_code=200)
    if x_github_event not in ("push", "pull_request"):
        return _ignored(f"event '{x_github_event}' is not handled")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="body is not valid JSON") from exc

    try:
        task_name, event = prepare_task(x_github_event, payload, x_github_delivery)
    except Ignored as exc:
        return _ignored(str(exc))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422, detail=f"malformed {x_github_event} payload: {exc}"
        ) from exc

    # GitHub redeliveries reuse the delivery GUID; SET NX makes processing at-most-once per id.
    dedupe_key = f"codelens:delivery:{x_github_delivery}" if x_github_delivery else None
    if dedupe_key:
        first_time = await redis.set(
            dedupe_key, "1", nx=True, ex=settings.delivery_dedupe_ttl_seconds
        )
        if not first_time:
            return {"status": "duplicate", "delivery_id": x_github_delivery}

    try:
        # Publishing to the broker is blocking network I/O; keep it off the event loop.
        await run_in_threadpool(enqueue, task_name, event)
    except Exception as exc:
        if dedupe_key:
            await redis.delete(dedupe_key)  # let a manual redelivery succeed later
        logger.exception("failed to enqueue %s %s", x_github_event, x_github_delivery)
        raise HTTPException(status_code=503, detail="queue unavailable") from exc

    response: dict[str, Any] = {
        "status": "accepted",
        "delivery_id": x_github_delivery,
        "task": task_name,
    }
    if "commits" in event:
        response["commits"] = len(event["commits"])
    else:
        response["pull_request"] = event["number"]
    return response
