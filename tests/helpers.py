from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Commit, Repo, Review, ReviewResult
from app.sources.local_git import LocalGitSource


def seed_reviews(
    db: sessionmaker[Session],
    source: LocalGitSource,
    *,
    full_name: str,
    sha: str,
    ref: str = "refs/heads/main",
    github_id: int = 1,
) -> list[int]:
    """Create repo/commit/review rows for a commit, the way the review_commit task does."""
    with db() as session:
        repo_id = session.execute(
            insert(Repo)
            .values(github_id=github_id, full_name=full_name, default_branch="main")
            .on_conflict_do_update(index_elements=["github_id"], set_={"full_name": full_name})
            .returning(Repo.id)
        ).scalar_one()
        commit_id = session.execute(
            insert(Commit)
            .values(repo_id=repo_id, sha=sha, ref=ref, message="test commit")
            .returning(Commit.id)
        ).scalar_one()
        ids = []
        for f in source.get_commit_files(full_name, sha):
            if f.patch:
                ids.append(
                    session.execute(
                        insert(Review)
                        .values(
                            repo_id=repo_id, commit_id=commit_id, file_path=f.path, patch=f.patch
                        )
                        .returning(Review.id)
                    ).scalar_one()
                )
        session.commit()
        return ids


def load_review(db: sessionmaker[Session], review_id: int) -> tuple[Review, ReviewResult | None]:
    with db() as session:
        review = session.get(Review, review_id)
        assert review is not None
        result = session.get(ReviewResult, review.result_id) if review.result_id else None
        return review, result


def result_count(db: sessionmaker[Session]) -> int:
    with db() as session:
        return len(session.execute(select(ReviewResult.id)).all())
