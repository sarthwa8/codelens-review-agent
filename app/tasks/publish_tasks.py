"""Publishing a review unit's results to GitHub, exactly once, after every file review is finished.

Triggers (all idempotent; extra triggers are cheap no-ops):
* a unit's files are listed and none need reviewing (all skipped);
* any review_file task finishes — including for *other* units whose reviews share the same cached
  result, because a follower's review only completes when the owner's generation completes.

The claim ``UPDATE ... WHERE publish_status = 'pending' AND NOT EXISTS (unfinished reviews)`` is
atomic, so concurrent triggers can't post twice.
"""

import logging

from celery import Task
from sqlalchemy import exists, func, or_, select, update
from sqlalchemy.orm import Session
from sqlalchemy.sql.selectable import Exists

from app.celery_app import celery_app
from app.db.models import Commit, PublishStatus, Repo, Review, ReviewResult, ReviewStatus
from app.github.publisher import PublishTarget, ReviewPublisher, get_publisher
from app.review.pipeline import PipelineDeps
from app.review.report import FileReport, build_report
from app.sources.base import SourceError
from app.tasks.common import backoff
from app.tasks.deps import get_pipeline_deps

logger = logging.getLogger(__name__)


def get_unit_publisher(deps: PipelineDeps) -> ReviewPublisher:
    return get_publisher(deps.github, deps.settings.public_url)


def _target(unit: Commit, full_name: str) -> PublishTarget:
    return PublishTarget(
        unit_id=unit.id,
        full_name=full_name,
        sha=unit.sha,
        kind=unit.kind,
        pr_number=unit.pr_number,
        check_run_id=unit.check_run_id,
    )


def start_check_run(deps: PipelineDeps, commit_id: int) -> None:
    """Show an in-progress check on GitHub as soon as a unit's files are known. Best effort."""
    publisher = get_unit_publisher(deps)
    if not publisher.enabled:
        return
    with deps.sessionmaker() as session:
        row = session.execute(
            select(Commit, Repo.full_name)
            .join(Repo, Commit.repo_id == Repo.id)
            .where(Commit.id == commit_id)
        ).one_or_none()
        if row is None or row[0].check_run_id or row[0].publish_status != PublishStatus.PENDING:
            return
        unit, full_name = row
        try:
            run_id = publisher.start(_target(unit, full_name))
        except SourceError as exc:
            logger.warning("could not create check run for unit %s: %s", commit_id, exc)
            return
        session.execute(update(Commit).where(Commit.id == commit_id).values(check_run_id=run_id))
        session.commit()


def schedule_publish_for_review(deps: PipelineDeps, review_id: int) -> None:
    with deps.sessionmaker() as session:
        result_id = select(Review.result_id).where(Review.id == review_id).scalar_subquery()
        unit_ids = (
            session.execute(
                select(Review.commit_id)
                .where(or_(Review.id == review_id, Review.result_id == result_id))
                .distinct()
            )
            .scalars()
            .all()
        )
    for unit_id in unit_ids:
        publish_unit.delay(unit_id)


def _unfinished_reviews(commit_id: int) -> Exists:
    return exists().where(
        Review.commit_id == commit_id, Review.status.not_in(ReviewStatus.TERMINAL)
    )


def _file_reports(session: Session, commit_id: int) -> list[FileReport]:
    rows = session.execute(
        select(Review, ReviewResult.review_text)
        .outerjoin(ReviewResult, Review.result_id == ReviewResult.id)
        .where(Review.commit_id == commit_id)
    ).all()
    return [
        FileReport(
            review_id=review.id,
            path=review.file_path,
            status=review.status,
            cache_hit=review.cache_hit,
            review_text=text or "",
            patch=review.patch,
            skip_reason=review.skip_reason,
            error=review.error,
        )
        for review, text in rows
    ]


@celery_app.task(name="codelens.publish_unit", bind=True, max_retries=5)
def publish_unit(self: Task, commit_id: int) -> str:
    deps = get_pipeline_deps()
    publisher = get_unit_publisher(deps)
    with deps.sessionmaker() as session:
        ready = (
            Commit.id == commit_id,
            Commit.publish_status == PublishStatus.PENDING,
            ~_unfinished_reviews(commit_id),
        )
        if not publisher.enabled:
            session.execute(
                update(Commit).where(*ready).values(publish_status=PublishStatus.SKIPPED)
            )
            session.commit()
            return "skipped"
        claimed = session.execute(
            update(Commit)
            .where(*ready)
            .values(publish_status=PublishStatus.PUBLISHING)
            .returning(Commit.id)
        ).scalar_one_or_none()
        session.commit()
        if claimed is None:
            return "not_ready"
        unit, full_name = session.execute(
            select(Commit, Repo.full_name)
            .join(Repo, Commit.repo_id == Repo.id)
            .where(Commit.id == commit_id)
        ).one()
        files = _file_reports(session, commit_id)

    report = build_report(files, unit_id=commit_id, public_url=deps.settings.public_url)
    try:
        publisher.finish(_target(unit, full_name), report)
    except SourceError as exc:
        will_retry = exc.retryable and self.request.retries < self.max_retries
        with deps.sessionmaker() as session:
            session.execute(
                update(Commit)
                .where(Commit.id == commit_id)
                .values(
                    publish_status=PublishStatus.PENDING if will_retry else PublishStatus.FAILED,
                    publish_error=str(exc)[:4000],
                )
            )
            session.commit()
        if will_retry:
            raise self.retry(exc=exc, countdown=backoff(self.request.retries, exc)) from exc
        logger.error("publishing unit %s failed: %s", commit_id, exc)
        return "failed"

    with deps.sessionmaker() as session:
        session.execute(
            update(Commit)
            .where(Commit.id == commit_id)
            .values(
                publish_status=PublishStatus.PUBLISHED, published_at=func.now(), publish_error=None
            )
        )
        session.commit()
    return "published"
