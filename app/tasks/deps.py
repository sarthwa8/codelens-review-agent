"""Per-worker-process dependency container."""

import logging
from functools import lru_cache

from celery.signals import worker_process_init

from app.config import get_settings
from app.db.session import get_sessionmaker
from app.llm import get_provider
from app.rag.chroma_store import CodeIndex, chroma_client_factory
from app.rag.embeddings import get_embedder
from app.rag.retriever import Retriever
from app.redis_client import get_sync_redis
from app.review.pipeline import PipelineDeps
from app.sources import get_source


@lru_cache
def get_pipeline_deps() -> PipelineDeps:
    settings = get_settings()
    factory = chroma_client_factory(
        settings.chroma_mode, settings.chroma_host, settings.chroma_port
    )
    index = CodeIndex(factory, get_embedder(settings)) if factory else None
    retriever = (
        Retriever(
            index,
            top_k=settings.rag_top_k,
            max_distance=settings.rag_max_distance,
            max_queries=settings.rag_max_queries,
        )
        if index
        else None
    )
    return PipelineDeps(
        sessionmaker=get_sessionmaker(),
        source=get_source(settings),
        llm=get_provider(settings),
        redis=get_sync_redis(),
        settings=settings,
        index=index,
        retriever=retriever,
    )


logger = logging.getLogger(__name__)


@worker_process_init.connect
def warm_up(**_: object) -> None:
    """Build dependencies and load the embedding model as each worker process starts.

    Otherwise the first review in every process pays several seconds of ONNX model loading before
    its first token. Lives here (not in celery_app) because the API process imports celery_app.
    """
    try:
        deps = get_pipeline_deps()
        if deps.index is not None:
            deps.index.warm_up()
    except Exception:  # never block worker start-up; the first task will retry lazily
        logger.warning("worker warm-up failed", exc_info=True)
