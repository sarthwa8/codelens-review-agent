"""SQLAlchemy models.

Caching and auditing are deliberately split across two tables:

* ``review_results`` holds one row per *unique cache key* — this is the cache, and its
  UNIQUE constraint doubles as the cross-worker lock that guarantees one LLM call per key.
* ``reviews`` holds one row per (commit, file) that was ever reviewed — this is the audit
  log. Cache hits still insert a row here (``cache_hit=True``) pointing at the shared result.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import false as sa_false
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class IndexStatus:
    NONE = "none"
    INDEXING = "indexing"
    READY = "ready"
    FAILED = "failed"
    ALL = (NONE, INDEXING, READY, FAILED)


class ResultStatus:
    PENDING = "pending"
    STREAMING = "streaming"
    COMPLETE = "complete"
    FAILED = "failed"
    ALL = (PENDING, STREAMING, COMPLETE, FAILED)


class ReviewStatus:
    PENDING = "pending"
    STREAMING = "streaming"
    COMPLETE = "complete"
    FAILED = "failed"
    SKIPPED = "skipped"
    ALL = (PENDING, STREAMING, COMPLETE, FAILED, SKIPPED)
    TERMINAL = (COMPLETE, FAILED, SKIPPED)


def _check_in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Repo(Base):
    __tablename__ = "repos"
    __table_args__ = (
        CheckConstraint(_check_in("index_status", IndexStatus.ALL), "ck_repos_index_status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # GitHub's numeric id is stable across renames/transfers; full_name is not.
    github_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    full_name: Mapped[str] = mapped_column(String(255), unique=True)
    default_branch: Mapped[str] = mapped_column(String(255), default="main", server_default="main")
    index_status: Mapped[str] = mapped_column(
        String(16), default=IndexStatus.NONE, server_default=IndexStatus.NONE
    )
    index_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    indexed_sha: Mapped[str | None] = mapped_column(String(40))
    indexed_chunks: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    embedding_model: Mapped[str | None] = mapped_column(String(128))
    index_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    commits: Mapped[list["Commit"]] = relationship(back_populates="repo")


class Commit(Base):
    """A commit *as pushed to a ref*. The same SHA pushed to two branches is two rows, so the
    audit trail records both pushes (and the second is typically served from cache)."""

    __tablename__ = "commits"
    __table_args__ = (
        UniqueConstraint("repo_id", "sha", "ref", name="uq_commits_repo_sha_ref"),
        Index("ix_commits_repo_id_id", "repo_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"))
    sha: Mapped[str] = mapped_column(String(40))
    ref: Mapped[str] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(Text, default="", server_default="")
    author: Mapped[str | None] = mapped_column(String(255))
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    repo: Mapped[Repo] = relationship(back_populates="commits")
    reviews: Mapped[list["Review"]] = relationship(back_populates="commit")


class ReviewResult(Base):
    """Cache entry: the LLM output for one cache key, plus what it cost to produce."""

    __tablename__ = "review_results"
    __table_args__ = (
        CheckConstraint(_check_in("status", ResultStatus.ALL), "ck_review_results_status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cache_key: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(
        String(16), default=ResultStatus.PENDING, server_default=ResultStatus.PENDING
    )
    review_text: Mapped[str] = mapped_column(Text, default="", server_default="")
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(32))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    error: Mapped[str | None] = mapped_column(Text)
    # What the model actually saw (chunk names, retrieved snippet locations) — for auditability.
    context: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Review(Base):
    """Audit row: one per (commit, file). Never overwritten by later pushes."""

    __tablename__ = "reviews"
    __table_args__ = (
        UniqueConstraint("commit_id", "file_path", name="uq_reviews_commit_file"),
        CheckConstraint(_check_in("status", ReviewStatus.ALL), "ck_reviews_status"),
        Index("ix_reviews_repo_id_id", "repo_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"))
    commit_id: Mapped[int] = mapped_column(ForeignKey("commits.id", ondelete="CASCADE"))
    file_path: Mapped[str] = mapped_column(String(1024))
    change_type: Mapped[str] = mapped_column(
        String(16), default="modified", server_default="modified"
    )
    language: Mapped[str | None] = mapped_column(String(32))
    patch: Mapped[str | None] = mapped_column(Text)
    # SHA-256 of (file content + patch) as described in the spec. Indexed but NOT unique:
    # identical code reviewed on two commits legitimately produces two audit rows.
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(
        String(16), default=ReviewStatus.PENDING, server_default=ReviewStatus.PENDING
    )
    skip_reason: Mapped[str | None] = mapped_column(String(255))
    error: Mapped[str | None] = mapped_column(Text)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False, server_default=sa_false())
    result_id: Mapped[int | None] = mapped_column(
        ForeignKey("review_results.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    commit: Mapped[Commit] = relationship(back_populates="reviews")
    result: Mapped[ReviewResult | None] = relationship()
