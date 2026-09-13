"""Build and sign GitHub-shaped push webhooks from a local git repository (shared by scripts)."""

import hashlib
import hmac
import json
import subprocess
import uuid
from pathlib import Path

ZERO_SHA = "0" * 40


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def github_id_for(full_name: str) -> int:
    return int(hashlib.sha256(full_name.encode()).hexdigest()[:12], 16)


def commit_entry(repo: Path, sha: str) -> dict:
    fmt = git(repo, "show", "-s", "--format=%H%x00%B%x00%an%x00%aI", sha).split("\x00")
    return {
        "id": fmt[0],
        "message": fmt[1].strip(),
        "timestamp": fmt[3],
        "author": {"name": fmt[2], "username": fmt[2].lower().replace(" ", "-")},
    }


def build_push(
    repo: Path,
    full_name: str,
    ref: str,
    before: str,
    after: str,
    default_branch: str = "main",
    forced: bool = False,
) -> dict:
    if before == ZERO_SHA:
        shas = [after]
    else:
        shas = git(repo, "rev-list", "--reverse", f"{before}..{after}").split() or [after]
    commits = [commit_entry(repo, sha) for sha in shas]
    return {
        "ref": ref,
        "before": before,
        "after": after,
        "forced": forced,
        "deleted": False,
        "repository": {
            "id": github_id_for(full_name),
            "full_name": full_name,
            "default_branch": default_branch,
        },
        "commits": commits,
        "head_commit": commits[-1],
    }


def signed_request(
    payload: dict, secret: str, delivery_id: str | None = None
) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-GitHub-Event": "push",
        "X-GitHub-Delivery": delivery_id or str(uuid.uuid4()),
        "X-Hub-Signature-256": signature,
    }
    return body, headers
