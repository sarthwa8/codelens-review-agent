"""Housekeeping for GitHub sign-in."""

from celery import Task
from sqlalchemy import delete, select

from app.celery_app import celery_app
from app.db.models import User, UserSession
from app.tasks.deps import get_pipeline_deps


@celery_app.task(name="codelens.revoke_user_sessions", bind=True, max_retries=3)
def revoke_user_sessions(self: Task, event: dict[str, int]) -> int:
    """The user revoked CodeLens on GitHub: end every session of theirs right away."""
    deps = get_pipeline_deps()
    with deps.sessionmaker() as session:
        ids = (
            session.execute(
                select(UserSession.id)
                .join(User, UserSession.user_id == User.id)
                .where(User.github_id == event["github_user_id"])
            )
            .scalars()
            .all()
        )
        session.execute(delete(UserSession).where(UserSession.id.in_(ids)))
        session.commit()
    for sid in ids:
        deps.redis.delete(f"codelens:access:{sid}")  # cached repo access for that session
    return len(ids)
