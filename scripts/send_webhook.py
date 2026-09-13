#!/usr/bin/env python3
"""Send a signed GitHub push webhook for commits in a local repository.

The repository must live at .demo-repos/<owner>/<name> and the stack must run with
SOURCE_MODE=local, so the worker reads the same commits from disk instead of the GitHub API.

    python scripts/send_webhook.py acme/storefront                 # last commit on current branch
    python scripts/send_webhook.py acme/storefront --commits 3     # last three commits
    python scripts/send_webhook.py acme/storefront --ref feature/x
"""

import argparse
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from webhook_payload import ZERO_SHA, build_push, git, signed_request

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("full_name", help="owner/name under .demo-repos/")
    parser.add_argument("--ref", help="branch name (default: the repository's current branch)")
    parser.add_argument(
        "--commits", type=int, default=1, help="how many recent commits the push contains"
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("CODELENS_URL", "http://localhost:8000") + "/webhooks/github",
    )
    parser.add_argument(
        "--secret", default=os.environ.get("GITHUB_WEBHOOK_SECRET", "dev-secret-change-me")
    )
    parser.add_argument("--repos-dir", type=Path, default=ROOT / ".demo-repos")
    args = parser.parse_args()

    repo = args.repos_dir / args.full_name
    if not (repo / ".git").exists():
        sys.exit(f"not a git repository: {repo}")
    branch = args.ref or git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    after = git(repo, "rev-parse", branch)
    count = int(git(repo, "rev-list", "--count", after))
    before = git(repo, "rev-parse", f"{after}~{args.commits}") if args.commits < count else ZERO_SHA

    payload = build_push(repo, args.full_name, f"refs/heads/{branch}", before, after)
    body, headers = signed_request(payload, args.secret)
    response = httpx.post(args.url, content=body, headers=headers, timeout=10)
    print(
        f"{response.status_code} {response.text}  ({len(payload['commits'])} commit(s) on {branch})"
    )


if __name__ == "__main__":
    main()
