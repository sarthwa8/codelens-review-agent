"""RAG indexing tasks (routed to the dedicated ``index`` queue)."""

import logging

from celery import Task
from sqlalchemy import func, select, update

from app.celery_app import celery_app
from app.db.models import IndexStatus, Repo
from app.parsing.languages import skip_reason_for_path
from app.parsing.treesitter import CodeChunk, extract_index_chunks
from app.tasks.deps import get_pipeline_deps

logger = logging.getLogger(__name__)

ADD_BATCH = 256


@celery_app.task(name="codelens.index_repo", bind=True, max_retries=0)
def index_repo(self: Task, repo_id: int) -> dict[str, int]:
    deps = get_pipeline_deps()
    index = deps.index
    with deps.sessionmaker() as session:
        repo = session.get(Repo, repo_id)
        if repo is None or index is None:
            return {"chunks": 0}
        full_name, ref = repo.full_name, repo.default_branch

    total = 0
    try:
        index.reset(repo_id)
        batch: list[CodeChunk] = []
        for path, content in deps.source.iter_repo_files(
            full_name, ref, deps.settings.max_file_bytes
        ):
            if skip_reason_for_path(path):
                continue
            batch.extend(extract_index_chunks(path, content))
            if len(batch) >= ADD_BATCH:
                total += index.add_chunks(repo_id, batch)
                batch.clear()
        total += index.add_chunks(repo_id, batch)
    except Exception as exc:
        logger.exception("indexing %s failed", full_name)
        with deps.sessionmaker() as session:
            session.execute(
                update(Repo)
                .where(Repo.id == repo_id)
                .values(index_status=IndexStatus.FAILED, index_error=str(exc)[:4000])
            )
            session.commit()
        return {"chunks": total}

    with deps.sessionmaker() as session:
        session.execute(
            update(Repo)
            .where(Repo.id == repo_id)
            .values(
                index_status=IndexStatus.READY,
                indexed_at=func.now(),
                indexed_sha=None,
                indexed_chunks=total,
                embedding_model=index.embedding_model,
                index_error=None,
            )
        )
        session.commit()
    logger.info("indexed %s: %d chunks", full_name, total)
    return {"chunks": total}


@celery_app.task(name="codelens.update_index", bind=True, max_retries=3)
def update_index(self: Task, repo_id: int, shas: list[str], after_sha: str) -> dict[str, int]:
    """Incrementally apply default-branch commits: re-chunk changed files, drop removed ones."""
    deps = get_pipeline_deps()
    index = deps.index
    with deps.sessionmaker() as session:
        repo = session.execute(select(Repo).where(Repo.id == repo_id)).scalar_one_or_none()
    if repo is None or index is None or repo.index_status != IndexStatus.READY:
        return {"updated": 0}

    changed: set[str] = set()
    removed: set[str] = set()
    for sha in shas:
        for f in deps.source.get_commit_files(repo.full_name, sha):
            if f.status == "removed":
                removed.add(f.path)
                changed.discard(f.path)
            else:
                changed.add(f.path)
                removed.discard(f.path)
                if f.previous_path and f.status == "renamed":
                    removed.add(f.previous_path)
                    changed.discard(f.previous_path)

    index.delete_paths(repo_id, sorted(removed))
    updated = 0
    for path in sorted(changed):
        if skip_reason_for_path(path):
            continue
        # Content at the push's final commit: intermediate versions are already superseded.
        content = deps.source.get_file_content(
            repo.full_name, path, after_sha, deps.settings.max_file_bytes
        )
        if content is None:
            index.delete_paths(repo_id, [path])
            continue
        updated += index.replace_file(repo_id, path, extract_index_chunks(path, content))

    with deps.sessionmaker() as session:
        session.execute(
            update(Repo)
            .where(Repo.id == repo_id)
            .values(
                indexed_sha=after_sha, indexed_chunks=index.count(repo_id), indexed_at=func.now()
            )
        )
        session.commit()
    return {"updated": updated, "removed": len(removed)}
