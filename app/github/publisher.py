"""Post review results back to GitHub: a check run per unit, plus a PR review for pull requests.

Only GitHub Apps may create check runs, so publishing is enabled only with an App configured;
otherwise (or in local source mode) the NullPublisher records nothing and units are marked skipped.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from app.github.client import GitHubClient
from app.review.report import REVIEW_MARKER, UnitReport
from app.sources.base import SourceError

logger = logging.getLogger(__name__)

CHECK_NAME = "CodeLens"
ANNOTATIONS_PER_REQUEST = 50  # GitHub's limit; further PATCH requests append more


@dataclass(frozen=True)
class PublishTarget:
    unit_id: int
    full_name: str
    sha: str
    kind: str  # push | pull_request
    pr_number: int | None
    check_run_id: int | None


class ReviewPublisher(Protocol):
    enabled: bool

    def start(self, target: PublishTarget) -> int | None:
        """Create an in-progress check run; returns its id."""
        ...

    def finish(self, target: PublishTarget, report: UnitReport) -> None: ...


class NullPublisher:
    enabled = False

    def start(self, target: PublishTarget) -> int | None:
        return None

    def finish(self, target: PublishTarget, report: UnitReport) -> None:
        return None


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class GitHubPublisher:
    enabled = True

    def __init__(self, client: GitHubClient, public_url: str):
        self.client = client
        self.public_url = public_url.rstrip("/")

    def _json(self, method: str, path: str, repo: str, **kwargs: Any) -> Any:
        response = self.client.request(method, path, repo=repo, **kwargs)
        if response.is_error:
            raise SourceError(
                f"GitHub {method} {path} → {response.status_code}: {response.text[:300]}"
            )
        return response.json() if response.content else None

    def start(self, target: PublishTarget) -> int | None:
        body = self._json(
            "POST",
            f"/repos/{target.full_name}/check-runs",
            target.full_name,
            json={
                "name": CHECK_NAME,
                "head_sha": target.sha,
                "status": "in_progress",
                "started_at": _now(),
                "external_id": str(target.unit_id),
                "details_url": f"{self.public_url}/repos/{target.full_name}",
                "output": {"title": "Reviewing…", "summary": "CodeLens is reviewing this change."},
            },
        )
        return int(body["id"])

    def finish(self, target: PublishTarget, report: UnitReport) -> None:
        self._complete_check_run(target, report)
        if target.kind == "pull_request" and target.pr_number is not None:
            self._post_pull_request_review(target, report)

    def _complete_check_run(self, target: PublishTarget, report: UnitReport) -> None:
        batches = [
            report.annotations[i : i + ANNOTATIONS_PER_REQUEST]
            for i in range(0, len(report.annotations), ANNOTATIONS_PER_REQUEST)
        ] or [[]]
        output = {"title": report.title, "summary": report.summary, "text": report.text}
        completion = {
            "name": CHECK_NAME,
            "status": "completed",
            "conclusion": report.conclusion,
            "completed_at": _now(),
            "output": {**output, "annotations": [vars(a) for a in batches[0]]},
        }
        repo = target.full_name
        if target.check_run_id:
            run_id = target.check_run_id
            self._json("PATCH", f"/repos/{repo}/check-runs/{run_id}", repo, json=completion)
        else:
            created = self._json(
                "POST",
                f"/repos/{repo}/check-runs",
                repo,
                json={**completion, "head_sha": target.sha},
            )
            run_id = int(created["id"])
        for batch in batches[1:]:
            # Each update appends its annotations; title and summary are required with every output.
            self._json(
                "PATCH",
                f"/repos/{repo}/check-runs/{run_id}",
                repo,
                json={
                    "output": {
                        "title": report.title,
                        "summary": report.summary,
                        "annotations": [vars(a) for a in batch],
                    }
                },
            )

    def _post_pull_request_review(self, target: PublishTarget, report: UnitReport) -> None:
        repo, number = target.full_name, target.pr_number
        pull = self._json("GET", f"/repos/{repo}/pulls/{number}", repo)
        if pull["state"] != "open" or pull["head"]["sha"] != target.sha:
            logger.info(
                "PR %s#%s moved past %s; not posting a stale review", repo, number, target.sha
            )
            return
        marker = REVIEW_MARKER.format(unit_id=target.unit_id)
        existing = self._json(
            "GET", f"/repos/{repo}/pulls/{number}/reviews", repo, params={"per_page": 100}
        )
        if any(marker in (review.get("body") or "") for review in existing or []):
            return  # already posted (e.g. the task was redelivered after a crash)

        comments = [
            {"path": c.path, "line": c.line, "side": "RIGHT", "body": c.body}
            for c in report.inline_comments
        ]
        payload = {
            "commit_id": target.sha,
            "event": "COMMENT",
            "body": report.review_body,
            "comments": comments,
        }
        path = f"/repos/{repo}/pulls/{number}/reviews"
        response = self.client.request("POST", path, repo=repo, json=payload)
        if response.status_code == 422 and comments:
            # A comment GitHub can't anchor rejects the whole review; keep the findings in the body.
            logger.warning(
                "inline comments rejected for %s#%s: %s", repo, number, response.text[:300]
            )
            moved = "\n".join(f"- `{c['path']}:{c['line']}`: {c['body']}" for c in comments)
            body = report.review_body.replace(
                marker, f"**Findings on changed lines**\n{moved}\n\n{marker}"
            )
            response = self.client.request(
                "POST", path, repo=repo, json={**payload, "body": body, "comments": []}
            )
        if response.is_error:
            raise SourceError(f"GitHub POST {path} → {response.status_code}: {response.text[:300]}")


def get_publisher(client: GitHubClient | None, public_url: str) -> ReviewPublisher:
    if client is not None and client.is_app:
        return GitHubPublisher(client, public_url)
    return NullPublisher()
