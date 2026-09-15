"""Local git source provider.

Maps ``owner/name`` to ``<LOCAL_REPOS_DIR>/owner/name`` and answers the same questions the
GitHub API would, using ``git`` directly. Powers the offline demo, the replay benchmark, and
end-to-end tests without network access. Pull requests are modelled as ``base...head`` diffs.
"""

import subprocess
from collections.abc import Iterator
from pathlib import Path

from app.sources.base import CommitFile, SourceError

_STATUS = {
    "A": "added",
    "M": "modified",
    "D": "removed",
    "R": "renamed",
    "C": "copied",
    "T": "changed",
}


class LocalGitSource:
    def __init__(self, repos_dir: Path):
        self.repos_dir = repos_dir

    def _repo(self, full_name: str) -> Path:
        path = (self.repos_dir / full_name).resolve()
        if self.repos_dir.resolve() not in path.parents or not path.exists():
            raise SourceError(f"local repo not found: {full_name}")
        return path

    def _git(self, full_name: str, *args: str) -> bytes:
        result = subprocess.run(
            ["git", "-C", str(self._repo(full_name)), *args], capture_output=True, check=False
        )
        if result.returncode != 0:
            raise SourceError(
                f"git {' '.join(args[:2])} failed: {result.stderr.decode(errors='replace')[:500]}"
            )
        return result.stdout

    def _files(self, full_name: str, name_status: bytes, patch_args: list[str]) -> list[CommitFile]:
        files = []
        for line in name_status.decode().splitlines():
            parts = line.split("\t")
            code = parts[0][0]
            path, previous = (parts[2], parts[1]) if code in "RC" else (parts[1], None)
            patch = None
            if code != "D":
                diff = self._git(full_name, *patch_args, "--", path).decode(
                    "utf-8", errors="replace"
                )
                # Match GitHub's API, whose "patch" field starts at the first hunk header.
                start = diff.find("\n@@")
                patch = diff[start + 1 :] if start != -1 else None
            files.append(
                CommitFile(
                    path=path,
                    status=_STATUS.get(code, "modified"),
                    patch=patch,
                    previous_path=previous,
                )
            )
        return files

    def get_commit_files(self, full_name: str, sha: str) -> list[CommitFile]:
        # --root so the first commit of a repo also lists its files.
        names = self._git(
            full_name, "diff-tree", "--root", "--no-commit-id", "-r", "-M", "--name-status", sha
        )
        return self._files(full_name, names, ["show", "--format=", "--no-color", "-M", sha])

    def get_pull_request_files(
        self, full_name: str, number: int, base_sha: str, head_sha: str
    ) -> list[CommitFile]:
        # Three dots: changes on head since it forked from base, exactly what a PR shows.
        span = f"{base_sha}...{head_sha}"
        names = self._git(full_name, "diff", "-M", "--name-status", span)
        return self._files(full_name, names, ["diff", "--no-color", "-M", span])

    def find_open_pull_request(self, full_name: str, branch: str) -> int | None:
        return None  # local repositories have no pull requests

    def get_file_content(self, full_name: str, path: str, ref: str, max_bytes: int) -> str | None:
        try:
            data = self._git(full_name, "show", f"{ref}:{path}")
        except SourceError:
            return None
        if len(data) > max_bytes or b"\x00" in data[:8000]:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def iter_repo_files(
        self, full_name: str, ref: str, max_bytes: int
    ) -> Iterator[tuple[str, str]]:
        listing = self._git(full_name, "ls-tree", "-r", "--long", ref).decode()
        for line in listing.splitlines():
            meta, path = line.split("\t", 1)
            _mode, kind, _obj, size = meta.split()
            if kind != "blob" or size == "-" or int(size) > max_bytes:
                continue
            content = self.get_file_content(full_name, path, ref, max_bytes)
            if content is not None:
                yield path, content
