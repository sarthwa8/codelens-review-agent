from celery import Celery

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "codelens",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks.review_tasks", "app.tasks.index_tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    # Results live in Postgres; don't also accumulate them in Redis.
    task_ignore_result=True,
    # Ack only after the task finishes, and requeue if the worker process dies mid-task.
    # Safe because every task is idempotent (DB unique constraints + cache claims).
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_default_queue="review",
    # Full-repo indexing is slow; isolate it so it can never starve reviews.
    task_routes={
        "codelens.index_repo": {"queue": "index"},
        "codelens.update_index": {"queue": "index"},
    },
    # Must exceed the longest task, or Redis redelivers a still-running task.
    broker_transport_options={"visibility_timeout": 2 * 3600},
    broker_connection_retry_on_startup=True,
)
