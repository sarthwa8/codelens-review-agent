"""Read API over the review history. Every review ever produced — cached or generated — is here.

Every route is scoped to the viewer: with GitHub sign-in on, only repositories the signed-in account
can read on GitHub are visible. Hidden repositories answer 404, not 403, so the API doesn't reveal
which private repositories exist.

Handlers are sync ``def`` functions: FastAPI runs them in its threadpool, so blocking DB calls
never stall the event loop that serves webhooks and SSE streams.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Select, and_, func, select
from sqlalchemy.orm import Session

from app.api.schemas import CommitOut, Page, RepoOut, ResultOut, ReviewDetail, ReviewSummary, Stats
from app.auth.deps import get_viewer
from app.auth.sessions import Viewer
from app.db.models import Commit, Repo, Review, ReviewResult, ReviewStatus
from app.db.session import get_session

router = APIRouter(tags=["reviews"])
SessionDep = Annotated[Session, Depends(get_session)]
ViewerDep = Annotated[Viewer, Depends(get_viewer)]


def visible_repos(viewer: Viewer) -> Any:
    """SQL condition limiting ``Repo`` rows to what the viewer may see."""
    if viewer.repo_ids is None:
        return Repo.id.is_not(None)
    return Repo.github_id.in_(sorted(viewer.repo_ids))


def _repo_or_404(session: Session, owner: str, name: str, viewer: Viewer) -> Repo:
    repo = session.execute(
        select(Repo).where(Repo.full_name == f"{owner}/{name}", visible_repos(viewer))
    ).scalar_one_or_none()
    if repo is None:
        raise HTTPException(status_code=404, detail="repository not found")
    return repo


def _summary(review: Review, commit: Commit) -> ReviewSummary:
    return ReviewSummary(
        id=review.id,
        file_path=review.file_path,
        change_type=review.change_type,
        language=review.language,
        status=review.status,
        cache_hit=review.cache_hit,
        skip_reason=review.skip_reason,
        created_at=review.created_at,
        commit_sha=commit.sha,
        ref=commit.ref,
    )


@router.get("/repos", response_model=list[RepoOut])
def list_repos(session: SessionDep, viewer: ViewerDep) -> list[RepoOut]:
    activity = (
        select(
            Review.repo_id,
            func.count(Review.id).label("n"),
            func.max(Review.created_at).label("last"),
        )
        .group_by(Review.repo_id)
        .subquery()
    )
    rows = session.execute(
        select(Repo, activity.c.n, activity.c.last)
        .outerjoin(activity, activity.c.repo_id == Repo.id)
        .where(visible_repos(viewer), ~Repo.full_name.contains("#renamed-"))
        .order_by(func.coalesce(activity.c.last, Repo.created_at).desc())
    ).all()
    return [
        RepoOut.model_validate(repo).model_copy(
            update={"review_count": n or 0, "last_activity_at": last}
        )
        for repo, n, last in rows
    ]


@router.get("/repos/{owner}/{name}", response_model=RepoOut)
def get_repo(owner: str, name: str, session: SessionDep, viewer: ViewerDep) -> RepoOut:
    return RepoOut.model_validate(_repo_or_404(session, owner, name, viewer))


@router.get("/repos/{owner}/{name}/commits", response_model=Page[CommitOut])
def list_commits(
    owner: str,
    name: str,
    session: SessionDep,
    viewer: ViewerDep,
    limit: int = Query(20, ge=1, le=100),
    before_id: int | None = Query(None, description="cursor: return units older than this id"),
) -> Page[CommitOut]:
    repo = _repo_or_404(session, owner, name, viewer)
    query = select(Commit).where(Commit.repo_id == repo.id)
    if before_id is not None:
        query = query.where(Commit.id < before_id)
    commits = session.execute(query.order_by(Commit.id.desc()).limit(limit + 1)).scalars().all()
    page, has_more = commits[:limit], len(commits) > limit

    reviews_by_commit: dict[int, list[ReviewSummary]] = {c.id: [] for c in page}
    by_id = {c.id: c for c in page}
    if page:
        for review in session.execute(
            select(Review).where(Review.commit_id.in_(reviews_by_commit)).order_by(Review.file_path)
        ).scalars():
            reviews_by_commit[review.commit_id].append(_summary(review, by_id[review.commit_id]))

    return Page[CommitOut](
        items=[
            CommitOut(
                id=c.id,
                sha=c.sha,
                ref=c.ref,
                kind=c.kind,
                pr_number=c.pr_number,
                skip_reason=c.skip_reason,
                publish_status=c.publish_status,
                message=c.message,
                author=c.author,
                committed_at=c.committed_at,
                created_at=c.created_at,
                reviews=reviews_by_commit[c.id],
            )
            for c in page
        ],
        next_before_id=page[-1].id if has_more else None,
    )


@router.get("/repos/{owner}/{name}/reviews", response_model=Page[ReviewSummary])
def list_reviews(
    owner: str,
    name: str,
    session: SessionDep,
    viewer: ViewerDep,
    commit_sha: str | None = None,
    status: str | None = Query(None, pattern="^(" + "|".join(ReviewStatus.ALL) + ")$"),
    cache_hit: bool | None = None,
    file_path: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    before_id: int | None = None,
) -> Page[ReviewSummary]:
    repo = _repo_or_404(session, owner, name, viewer)
    query: Select[tuple[Review, Commit]] = (
        select(Review, Commit)
        .join(Commit, Review.commit_id == Commit.id)
        .where(Review.repo_id == repo.id)
    )
    if commit_sha:
        query = query.where(Commit.sha.startswith(commit_sha))
    if status:
        query = query.where(Review.status == status)
    if cache_hit is not None:
        query = query.where(Review.cache_hit == cache_hit)
    if file_path:
        query = query.where(Review.file_path == file_path)
    if before_id is not None:
        query = query.where(Review.id < before_id)
    rows = session.execute(query.order_by(Review.id.desc()).limit(limit + 1)).all()
    page = rows[:limit]
    return Page[ReviewSummary](
        items=[_summary(review, commit) for review, commit in page],
        next_before_id=page[-1][0].id if len(rows) > limit else None,
    )


@router.get("/reviews/{review_id}", response_model=ReviewDetail)
def get_review(review_id: int, session: SessionDep, viewer: ViewerDep) -> ReviewDetail:
    row = session.execute(
        select(Review, Commit, Repo, ReviewResult)
        .join(Commit, Review.commit_id == Commit.id)
        .join(Repo, Review.repo_id == Repo.id)
        .outerjoin(ReviewResult, Review.result_id == ReviewResult.id)
        .where(Review.id == review_id, visible_repos(viewer))
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="review not found")
    review, commit, repo, result = row
    return ReviewDetail(
        **_summary(review, commit).model_dump(),
        repo_full_name=repo.full_name,
        kind=commit.kind,
        pr_number=commit.pr_number,
        commit_message=commit.message,
        patch=review.patch,
        content_hash=review.content_hash,
        error=review.error,
        review_text=result.review_text if result else "",
        result=ResultOut(
            id=result.id,
            status=result.status,
            provider=result.provider,
            model=result.model,
            prompt_version=result.prompt_version,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            latency_ms=result.latency_ms,
            attempts=result.attempts,
            context=result.context,
            completed_at=result.completed_at,
        )
        if result
        else None,
    )


@router.get("/stats", response_model=Stats)
def get_stats(
    session: SessionDep, viewer: ViewerDep, repo: str | None = Query(None, description="owner/name")
) -> Stats:
    """Measured cache effectiveness over the viewer's repositories. "LLM calls" counts reviews that
    generated a result; "cache hits" counts reviews served from an existing result without a call."""
    if repo:
        owner, _, name = repo.partition("/")
        scope = [Review.repo_id == _repo_or_404(session, owner, name, viewer).id]
    else:
        scope = [Review.repo_id.in_(select(Repo.id).where(visible_repos(viewer)))]

    by_status = dict(
        session.execute(select(Review.status, func.count()).where(*scope).group_by(Review.status))
        .tuples()
        .all()
    )
    attached = and_(Review.result_id.is_not(None), *scope)
    llm_calls = session.execute(
        select(func.count(Review.id)).where(attached, Review.cache_hit.is_(False))
    ).scalar_one()
    hits, tokens_saved, ms_saved = session.execute(
        select(
            func.count(Review.id),
            func.coalesce(
                func.sum(
                    func.coalesce(ReviewResult.input_tokens, 0)
                    + func.coalesce(ReviewResult.output_tokens, 0)
                ),
                0,
            ),
            func.coalesce(func.sum(ReviewResult.latency_ms), 0),
        )
        .select_from(Review)
        .join(ReviewResult, Review.result_id == ReviewResult.id)
        .where(attached, Review.cache_hit.is_(True))
    ).one()
    served = llm_calls + hits
    rate = round(hits / served, 4) if served else 0.0
    return Stats(
        reviews_total=sum(by_status.values()),
        by_status=by_status,
        llm_calls=llm_calls,
        cache_hits=hits,
        cache_hit_rate=rate,
        llm_calls_avoided_pct=round(rate * 100, 2),
        tokens_saved=int(tokens_saved),
        generation_ms_saved=int(ms_saved),
    )
