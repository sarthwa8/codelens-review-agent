import json

import httpx
import pytest

from app.config import Settings
from app.github.client import GitHubClient
from app.github.publisher import GitHubPublisher, PublishTarget
from app.review.report import Annotation, InlineComment, UnitReport
from app.sources.base import SourceError

HEAD = "a" * 40


class FakeGitHub:
    """Records writes; serves configurable PR state and existing reviews."""

    def __init__(
        self,
        *,
        pr_head: str = HEAD,
        pr_state: str = "open",
        existing_reviews=(),
        reject_comments=False,
    ):
        self.pr_head = pr_head
        self.pr_state = pr_state
        self.existing_reviews = list(existing_reviews)
        self.reject_comments = reject_comments
        self.writes: list[tuple[str, str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else {}
        if method != "GET":
            self.writes.append((method, path, body))
        if method == "POST" and path.endswith("/check-runs"):
            return httpx.Response(201, json={"id": 900})
        if method == "PATCH" and "/check-runs/" in path:
            return httpx.Response(200, json={"id": 900})
        if method == "GET" and path.endswith("/pulls/12"):
            return httpx.Response(200, json={"state": self.pr_state, "head": {"sha": self.pr_head}})
        if method == "GET" and path.endswith("/pulls/12/reviews"):
            return httpx.Response(200, json=self.existing_reviews)
        if method == "POST" and path.endswith("/pulls/12/reviews"):
            if self.reject_comments and body["comments"]:
                return httpx.Response(422, json={"message": "Line could not be resolved"})
            return httpx.Response(200, json={"id": 1})
        raise AssertionError(f"unexpected {method} {path}")

    def publisher(self) -> GitHubPublisher:
        http = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(self))
        client = GitHubClient(Settings(_env_file=None, github_token="t"), http=http)
        return GitHubPublisher(client, "https://codelens.example.com/")


def report(annotations: int = 0, comments: int = 1) -> UnitReport:
    return UnitReport(
        conclusion="neutral",
        title="1 finding (1 major)",
        summary="summary",
        text="full text",
        review_body="body\n\n<!-- codelens:unit:5 -->",
        annotations=[
            Annotation("a.py", i + 1, i + 1, "warning", "CodeLens: major", "m")
            for i in range(annotations)
        ],
        inline_comments=[InlineComment("a.py", 3, "**[major]** fix it") for _ in range(comments)],
    )


def target(kind: str = "pull_request", check_run_id: int | None = 900) -> PublishTarget:
    return PublishTarget(
        unit_id=5,
        full_name="acme/shop",
        sha=HEAD,
        kind=kind,
        pr_number=12,
        check_run_id=check_run_id,
    )


def test_start_creates_in_progress_check_run() -> None:
    github = FakeGitHub()
    assert github.publisher().start(target(check_run_id=None)) == 900
    [(method, path, body)] = github.writes
    assert (method, path) == ("POST", "/repos/acme/shop/check-runs")
    assert (body["name"], body["head_sha"], body["status"]) == ("CodeLens", HEAD, "in_progress")
    assert body["details_url"] == "https://codelens.example.com/repos/acme/shop"


def test_check_run_annotations_are_sent_in_batches_of_50() -> None:
    github = FakeGitHub()
    github.publisher().finish(target(kind="push"), report(annotations=120))
    patches = [body for method, path, body in github.writes if method == "PATCH"]
    assert [len(p["output"]["annotations"]) for p in patches] == [50, 50, 20]
    assert patches[0]["conclusion"] == "neutral" and patches[0]["status"] == "completed"
    assert all(p["output"]["title"] and p["output"]["summary"] for p in patches)
    assert not any(path.endswith("/reviews") for _, path, _ in github.writes)  # push: no PR review


def test_missing_check_run_is_created_completed() -> None:
    github = FakeGitHub()
    github.publisher().finish(target(kind="push", check_run_id=None), report())
    [(method, _, body)] = github.writes
    assert method == "POST" and body["head_sha"] == HEAD and body["status"] == "completed"


def test_pull_request_review_is_a_comment_with_inline_comments() -> None:
    github = FakeGitHub()
    github.publisher().finish(target(), report())
    [(_, _, review)] = [w for w in github.writes if w[1].endswith("/pulls/12/reviews")]
    assert (review["event"], review["commit_id"]) == ("COMMENT", HEAD)
    assert review["comments"] == [
        {"path": "a.py", "line": 3, "side": "RIGHT", "body": "**[major]** fix it"}
    ]


def test_stale_head_or_closed_pr_gets_no_review() -> None:
    for github in (FakeGitHub(pr_head="b" * 40), FakeGitHub(pr_state="closed")):
        github.publisher().finish(target(), report())
        assert not any(path.endswith("/reviews") for _, path, _ in github.writes)
        assert any(
            method == "PATCH" for method, _, _ in github.writes
        )  # the check run still completes


def test_review_is_not_posted_twice() -> None:
    github = FakeGitHub(existing_reviews=[{"body": "old\n<!-- codelens:unit:5 -->"}])
    github.publisher().finish(target(), report())
    assert not any(path.endswith("/reviews") for _, path, _ in github.writes)


def test_unanchorable_comments_fall_back_to_the_review_body() -> None:
    github = FakeGitHub(reject_comments=True)
    github.publisher().finish(target(), report())
    reviews = [body for _, path, body in github.writes if path.endswith("/pulls/12/reviews")]
    assert len(reviews) == 2 and reviews[1]["comments"] == []
    assert "`a.py:3`: **[major]** fix it" in reviews[1]["body"]
    assert reviews[1]["body"].rstrip().endswith("<!-- codelens:unit:5 -->")


def test_github_errors_surface_as_source_errors() -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "Resource not accessible by integration"})

    http = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(forbidden))
    publisher = GitHubPublisher(
        GitHubClient(Settings(_env_file=None, github_token="t"), http=http), "http://x"
    )
    with pytest.raises(SourceError, match="403"):
        publisher.start(target(check_run_id=None))
