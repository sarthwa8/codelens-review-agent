"""Source-code access abstraction: GitHub REST API in production, local git for demos/tests."""

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CommitFile:
    path: str
    status: str  # added | modified | removed | renamed | copied | changed
    patch: str | None  # None when GitHub omits it (binary or diff too large)
    previous_path: str | None = None


class SourceError(Exception):
    def __init__(self, message: str, *, retryable: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class SourceProvider(Protocol):
    def get_commit_files(self, full_name: str, sha: str) -> list[CommitFile]: ...

    def get_file_content(self, full_name: str, path: str, ref: str, max_bytes: int) -> str | None:
        """Decoded UTF-8 file content, or None if missing, binary, or larger than ``max_bytes``."""
        ...

    def iter_repo_files(
        self, full_name: str, ref: str, max_bytes: int
    ) -> Iterator[tuple[str, str]]:
        """Yield (path, content) for every text file at ``ref`` (used for RAG indexing)."""
        ...
