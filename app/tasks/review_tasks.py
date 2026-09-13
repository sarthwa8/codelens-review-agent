"""Celery tasks for the review flow.

    process_push ─┬─▶ review_commit (per commit) ──▶ review_file (per changed file)
                  ├─▶ index_repo   (first push: full default-branch index, own queue)
                  └─▶ update_index (default-branch pushes only)

Fan-out keeps one slow/broken file from delaying or failing the rest of the push, and lets
review_file retries be scoped to a single file.
"""

import logging
import random
from datetime import datetime, timedelta
from typing import Any

from celery import Task
from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.db.models import Commit, IndexStatus, Repo, Review, ReviewStatus
from app.llm.adapter import LLMError
from app.parsing.languages import detect_language, skip_reason_for_path
from app.rag.embeddings import embedder_name
from app.review import pipeline
from app.sources.base import SourceError
from app.tasks.deps import get_pipeline_deps
from app.tasks.index_tasks import index_repo, update_index

logger = logging.getLogger(__name__)

INDEX_STALE_AFTER = timedelta(hours=2)
INDEX_RETRY_AFTER_FAILURE = timedelta(minutes=15)
REPO_UPSERT_ATTEMPTS = 3


def _backoff(retries: int, exc: Exception) -> float:
    hint = getattr(exc, "retry_after", None)
    if hint:
        return float(hint)
    return min(300.0, 5 * 2**retries) + random.uniform(0, 3)  # jitter avoids synchronized retries


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def upsert_repo(session: Session, repo_info: dict[str, Any]) -> int:
    """Insert or refresh a repository row, keyed by GitHub's stable numeric id.

    ``ON CONFLICT (github_id)`` only arbitrates that one constraint. Two things can still violate
    the UNIQUE ``full_name``:

    * concurrent first pushes for a new repo — Postgres may detect the ``full_name`` duplicate
      before the arbiter index, raising instead of taking the DO UPDATE path. Retrying resolves it,
      because the row is then visible and the upsert updates it.
    * a repo renamed away and a *different* repo later created under the old name — the stale row
      gives up the name (it keeps its history under a tombstoned name).
    """
    for attempt in range(1, REPO_UPSERT_ATTEMPTS + 1):
        try:
            session.execute(
                update(Repo)
                .where(
                    Repo.full_name == repo_info["full_name"],
                    Repo.github_id != repo_info["github_id"],
                )
                .values(full_name=func.concat(Repo.full_name, "#renamed-", Repo.id))
            )
            repo_id = session.execute(
                insert(Repo)
                .values(
                    github_id=repo_info["github_id"],
                    full_name=repo_info["full_name"],
                    default_branch=repo_info["default_branch"],
                )
                .on_conflict_do_update(
                    index_elements=["github_id"],
                    set_={
                        "full_name": repo_info["full_name"],
                        "default_branch": repo_info["default_branch"],
                    },
                )
                .returning(Repo.id)
            ).scalar_one()
            session.commit()
            return int(repo_id)
        except IntegrityError:
            session.rollback()
            if attempt == REPO_UPSERT_ATTEMPTS:
                raise
    raise AssertionError("unreachable")


@celery_app.task(name="codelens.process_push", bind=True, max_retries=5)
def process_push(self: Task, event: dict[str, Any]) -> dict[str, Any]:
    deps = get_pipeline_deps()
    repo_info = event["repo"]
    with deps.sessionmaker() as session:
        repo_id = upsert_repo(session, repo_info)

        commit_ids = []
        for commit in event["commits"]:
            values = {
                "repo_id": repo_id,
                "sha": commit["sha"],
                "ref": event["ref"],
                "message": commit.get("message") or "",
                "author": commit.get("author"),
                "committed_at": _parse_timestamp(commit.get("timestamp")),
                "delivery_id": event.get("delivery_id"),
            }
            session.execute(insert(Commit).values(**values).on_conflict_do_nothing())
            commit_ids.append(
                session.execute(
                    select(Commit.id).where(
                        Commit.repo_id == repo_id,
                        Commit.sha == commit["sha"],
                        Commit.ref == event["ref"],
                    )
                ).scalar_one()
            )

        start_index = False
        if deps.index is not None:
            expected_model = embedder_name(deps.settings)
            # Atomic check-and-set: concurrent pushes can't both start a full index.
            start_index = (
                session.execute(
                    update(Repo)
                    .where(
                        Repo.id == repo_id,
                        or_(
                            Repo.index_status == IndexStatus.NONE,
                            # Back off after a failure so a broken repo isn't re-indexed on every push.
                            (Repo.index_status == IndexStatus.FAILED)
                            & (Repo.index_started_at < func.now() - INDEX_RETRY_AFTER_FAILURE),
                            (Repo.index_status == IndexStatus.READY)
                            & (Repo.embedding_model != expected_model),
                            (Repo.index_status == IndexStatus.INDEXING)
                            & (Repo.index_started_at < func.now() - INDEX_STALE_AFTER),
                        ),
                    )
                    .values(
                        index_status=IndexStatus.INDEXING,
                        index_started_at=func.now(),
                        index_error=None,
                    )
                    .returning(Repo.id)
                ).scalar_one_or_none()
                is not None
            )
        index_ready = session.execute(
            select(Repo.index_status).where(Repo.id == repo_id)
        ).scalar_one() == (IndexStatus.READY)
        session.commit()

    if start_index:
        index_repo.delay(repo_id)
    for commit_id in commit_ids:
        review_commit.delay(commit_id)
    is_default_branch = event["ref"] == f"refs/heads/{repo_info['default_branch']}"
    if is_default_branch and index_ready and event.get("after"):
        update_index.delay(repo_id, [c["sha"] for c in event["commits"]], event["after"])
    return {"repo_id": repo_id, "commits": len(commit_ids), "index_started": start_index}


@celery_app.task(name="codelens.review_commit", bind=True, max_retries=5)
def review_commit(self: Task, commit_id: int) -> dict[str, int]:
    deps = get_pipeline_deps()
    with deps.sessionmaker() as session:
        row = session.execute(
            select(Commit.sha, Repo.full_name, Repo.id)
            .join(Repo, Commit.repo_id == Repo.id)
            .where(Commit.id == commit_id)
        ).one_or_none()
    if row is None:
        return {"reviews": 0}
    sha, full_name, repo_id = row

    try:
        files = deps.source.get_commit_files(full_name, sha)
    except SourceError as exc:
        if exc.retryable and self.request.retries < self.max_retries:
            raise self.retry(exc=exc, countdown=_backoff(self.request.retries, exc)) from exc
        logger.error("giving up on commit %s: %s", sha, exc)
        return {"reviews": 0}

    with deps.sessionmaker() as session:
        for f in files:
            reason: str | None
            if f.status == "removed":
                reason = "file removed"
            elif path_reason := skip_reason_for_path(f.path):
                reason = path_reason
            elif not f.patch:
                reason = "binary file, pure rename, or diff too large for GitHub"
            else:
                reason = None
            status = ReviewStatus.SKIPPED if reason else ReviewStatus.PENDING
            session.execute(
                insert(Review)
                .values(
                    repo_id=repo_id,
                    commit_id=commit_id,
                    file_path=f.path,
                    change_type=f.status,
                    language=detect_language(f.path),
                    patch=f.patch,
                    status=status,
                    skip_reason=reason,
                )
                .on_conflict_do_nothing(constraint="uq_reviews_commit_file")
            )
        session.commit()
        # Re-select rather than trusting inserted rows: if this task is redelivered after a crash
        # between insert and enqueue, still-pending reviews get enqueued again (review_file is idempotent).
        pending = (
            session.execute(
                select(Review.id).where(
                    Review.commit_id == commit_id, Review.status == ReviewStatus.PENDING
                )
            )
            .scalars()
            .all()
        )

    for review_id in pending:
        review_file.delay(review_id)
    return {"reviews": len(pending)}


@celery_app.task(name="codelens.review_file", bind=True, max_retries=3)
def review_file(self: Task, review_id: int) -> str:
    deps = get_pipeline_deps()
    final_attempt = self.request.retries >= self.max_retries
    try:
        return pipeline.review_file(review_id, deps, final_attempt=final_attempt)
    except (LLMError, SourceError) as exc:
        if exc.retryable and not final_attempt:
            raise self.retry(exc=exc, countdown=_backoff(self.request.retries, exc)) from exc
        pipeline.mark_review_failed(deps.sessionmaker, review_id, str(exc))
        logger.error("review %s failed permanently: %s", review_id, exc)
        return "failed"
    except Exception as exc:
        pipeline.mark_review_failed(deps.sessionmaker, review_id, f"internal error: {exc}")
        logger.exception("review %s crashed", review_id)
        return "failed"
