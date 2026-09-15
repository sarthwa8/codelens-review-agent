"""GitHub REST API source provider (sync httpx; runs inside Celery workers)."""

import io
import logging
import tarfile
import time
from collections.abc import Iterator
from urllib.parse import quote

import httpx

from app.sources.base import CommitFile, SourceError

logger = logging.getLogger(__name__)

MAX_COMMIT_FILE_PAGES = 30  # GitHub caps a commit's file list at 3000 files (100/page)


def _decode(data: bytes) -> str | None:
    if b"\x00" in data[:8000]:
        return None  # binary
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


class GitHubSource:
    def __init__(
        self,
        token: str | None,
        base_url: str = "https://api.github.com",
        client: httpx.Client | None = None,
    ):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = client or httpx.Client(
            base_url=base_url, headers=headers, timeout=30, follow_redirects=True
        )

    def _get(self, url: str, **kwargs: object) -> httpx.Response:
        try:
            response = self._client.get(url, **kwargs)  # type: ignore[arg-type]
        except httpx.TransportError as exc:
            raise SourceError(f"GitHub unreachable: {exc}", retryable=True) from exc
        if response.status_code in (403, 429) and (
            response.headers.get("x-ratelimit-remaining") == "0"
            or "retry-after" in response.headers
        ):
            retry_after = float(response.headers.get("retry-after") or 0) or max(
                float(response.headers.get("x-ratelimit-reset", time.time() + 60)) - time.time(), 1
            )
            raise SourceError(
                "GitHub rate limit exceeded",
                retryable=True,
                retry_after=retry_after,
                rate_limited=True,
            )
        if response.status_code >= 500:
            raise SourceError(f"GitHub {response.status_code}", retryable=True)
        return response

    def get_commit_files(self, full_name: str, sha: str) -> list[CommitFile]:
        files: list[CommitFile] = []
        for page in range(1, MAX_COMMIT_FILE_PAGES + 1):
            response = self._get(
                f"/repos/{full_name}/commits/{sha}", params={"per_page": 100, "page": page}
            )
            if response.status_code == 404:
                raise SourceError(f"commit {sha} not found in {full_name}")
            response.raise_for_status()
            batch = response.json().get("files") or []
            files.extend(
                CommitFile(
                    path=f["filename"],
                    status=f.get("status", "modified"),
                    patch=f.get("patch"),
                    previous_path=f.get("previous_filename"),
                )
                for f in batch
            )
            if len(batch) < 100:
                break
        return files

    def get_file_content(self, full_name: str, path: str, ref: str, max_bytes: int) -> str | None:
        response = self._get(
            f"/repos/{full_name}/contents/{quote(path)}",
            params={"ref": ref},
            headers={"Accept": "application/vnd.github.raw+json"},
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        if len(response.content) > max_bytes:
            return None
        return _decode(response.content)

    def iter_repo_files(
        self, full_name: str, ref: str, max_bytes: int
    ) -> Iterator[tuple[str, str]]:
        # One tarball request instead of thousands of contents-API calls.
        response = self._get(f"/repos/{full_name}/tarball/{ref}")
        response.raise_for_status()
        with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as archive:
            for member in archive:
                if not member.isfile() or member.size > max_bytes:
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                text = _decode(handle.read())
                if text is not None:
                    # Strip GitHub's "<owner>-<repo>-<sha>/" top-level directory.
                    yield member.name.split("/", 1)[-1], text
