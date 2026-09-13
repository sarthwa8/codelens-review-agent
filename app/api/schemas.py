from datetime import datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class RepoOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    full_name: str
    default_branch: str
    index_status: str
    indexed_chunks: int
    indexed_at: datetime | None
    embedding_model: str | None
    index_error: str | None
    review_count: int = 0
    last_activity_at: datetime | None = None


class ReviewSummary(BaseModel):
    id: int
    file_path: str
    change_type: str
    language: str | None
    status: str
    cache_hit: bool
    skip_reason: str | None
    created_at: datetime
    commit_sha: str
    ref: str


class CommitOut(BaseModel):
    id: int
    sha: str
    ref: str
    message: str
    author: str | None
    committed_at: datetime | None
    created_at: datetime
    reviews: list[ReviewSummary]


class ResultOut(BaseModel):
    id: int
    status: str
    provider: str
    model: str
    prompt_version: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int | None
    attempts: int
    context: dict[str, Any] | None
    completed_at: datetime | None


class ReviewDetail(ReviewSummary):
    repo_full_name: str
    commit_message: str
    patch: str | None
    content_hash: str | None
    error: str | None
    review_text: str
    result: ResultOut | None


class Page(BaseModel, Generic[T]):
    items: list[T]
    next_before_id: int | None


class Stats(BaseModel):
    reviews_total: int
    by_status: dict[str, int]
    llm_calls: int
    cache_hits: int
    cache_hit_rate: float
    llm_calls_avoided_pct: float
    tokens_saved: int
    generation_ms_saved: int
