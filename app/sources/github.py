"""GitHub REST API source provider (runs inside Celery workers)."""

import io
import logging
import tarfile
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote

from app.github.client import GitHubClient
from app.sources.base import CommitFile, SourceError

logger = logging.getLogger(__name__)

MAX_FILE_PAGES = 30  # GitHub caps commit and pull-request file lists at 3000 files (100/page)


def _decode(data: bytes) -> str | None:
    if b"\x00" in data[:8000]:
        return None  # binary
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _commit_file(entry: dict[str, Any]) -> CommitFile:
    return CommitFile(
        path=entry["filename"],
        status=entry.get("status", "modified"),
        patch=entry.get("patch"),  # absent for binary files and oversized diffs
        previous_path=entry.get("previous_filename"),
    )


class GitHubSource:
    def __init__(self, client: GitHubClient):
        self.client = client

    def _paged_files(self, full_name: str, path: str, *, key: str | None) -> list[CommitFile]:
        files: list[CommitFile] = []
        for page in range(1, MAX_FILE_PAGES + 1):
            response = self.client.request(
                "GET", path, repo=full_name, params={"per_page": 100, "page": page}
            )
            if response.status_code == 404:
                raise SourceError(f"{path} not found")
            body = self.client.json_or_raise(response)
            batch = (body.get(key) or []) if key else body
            files.extend(_commit_file(entry) for entry in batch)
            if len(batch) < 100:
                break
        return files

    def get_commit_files(self, full_name: str, sha: str) -> list[CommitFile]:
        return self._paged_files(full_name, f"/repos/{full_name}/commits/{sha}", key="files")

    def get_pull_request_files(
        self, full_name: str, number: int, base_sha: str, head_sha: str
    ) -> list[CommitFile]:
        return self._paged_files(full_name, f"/repos/{full_name}/pulls/{number}/files", key=None)

    def find_open_pull_request(self, full_name: str, branch: str) -> int | None:
        owner = full_name.split("/", 1)[0]
        response = self.client.request(
            "GET",
            f"/repos/{full_name}/pulls",
            repo=full_name,
            # head=owner:branch only matches branches in this repo, so a fork's PR from a
            # same-named branch never suppresses reviews of this repo's pushes.
            params={"head": f"{owner}:{branch}", "state": "open", "per_page": 1},
        )
        pulls = self.client.json_or_raise(response)
        return int(pulls[0]["number"]) if pulls else None

    def get_file_content(self, full_name: str, path: str, ref: str, max_bytes: int) -> str | None:
        response = self.client.request(
            "GET",
            f"/repos/{full_name}/contents/{quote(path)}",
            repo=full_name,
            params={"ref": ref},
            headers={"Accept": "application/vnd.github.raw+json"},
        )
        if response.status_code == 404:
            return None
        if response.is_error:
            raise SourceError(f"GitHub {response.status_code} fetching {path}@{ref}")
        if len(response.content) > max_bytes:
            return None
        return _decode(response.content)

    def iter_repo_files(
        self, full_name: str, ref: str, max_bytes: int
    ) -> Iterator[tuple[str, str]]:
        # One tarball request instead of thousands of contents-API calls.
        response = self.client.request("GET", f"/repos/{full_name}/tarball/{ref}", repo=full_name)
        if response.is_error:
            raise SourceError(f"tarball download failed: {response.status_code}")
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
