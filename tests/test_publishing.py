"""When and how often review units are published back to GitHub."""

import contextlib
from dataclasses import dataclass, field

import pytest
from celery.exceptions import Retry
from sqlalchemy import select, update

from app.db.models import Commit, PublishStatus, Review, ReviewStatus
from app.github.publisher import PublishTarget
from app.review.report import UnitReport
from app.sources.base import SourceError
from app.tasks import publish_tasks, review_tasks

FILES = {
    "shop/orders.py": "def total(items):\n    return sum(i.price for i in items)\n",
    "shop/users.py": "def name(user):\n    return user.login\n",
}


@dataclass
class RecordingPublisher:
    enabled: bool = True
    fail_with: SourceError | None = None
    started: list[PublishTarget] = field(default_factory=list)
    finished: list[tuple[PublishTarget, UnitReport]] = field(default_factory=list)

    def start(self, target: PublishTarget) -> int | None:
        self.started.append(target)
        return 1000 + target.unit_id

    def finish(self, target: PublishTarget, report: UnitReport) -> None:
        if self.fail_with:
            raise self.fail_with
        self.finished.append((target, report))


@pytest.fixture
def publisher(e2e, monkeypatch) -> RecordingPublisher:
    recorder = RecordingPublisher()
    monkeypatch.setattr(publish_tasks, "get_unit_publisher", lambda deps: recorder)
    return recorder


def push(git_repo, sha: str, ref: str = "refs/heads/main") -> dict:
    return {
        "delivery_id": f"{ref}-{sha}",
        "installation_id": None,
        "repo": {"github_id": 99, "full_name": git_repo.full_name, "default_branch": "main"},
        "ref": ref,
        "before": "0" * 40,
        "after": sha,
        "forced": False,
        "commits": [{"sha": sha, "message": "change", "author": "dev"}],
    }


def unit(e2e, sha: str, ref: str = "refs/heads/main") -> Commit:
    with e2e.sessionmaker() as session:
        return session.execute(
            select(Commit).where(Commit.sha == sha, Commit.ref == ref)
        ).scalar_one()


def test_unit_publishes_once_after_every_file_is_reviewed(git_repo, e2e, publisher) -> None:
    sha = git_repo.commit(FILES)
    review_tasks.process_push.delay(push(git_repo, sha))

    [started] = publisher.started
    [(target, report)] = publisher.finished
    assert target.unit_id == started.unit_id and target.check_run_id == 1000 + target.unit_id
    assert target.kind == "push" and target.sha == sha
    assert sorted(
        line.split("`")[1] for line in report.summary.splitlines() if line.startswith("| [`")
    ) == sorted(FILES)
    saved = unit(e2e, sha)
    assert saved.publish_status == PublishStatus.PUBLISHED and saved.published_at is not None

    assert (
        publish_tasks.publish_unit.delay(saved.id).get() == "not_ready"
    )  # extra triggers are no-ops
    assert len(publisher.finished) == 1


def test_units_sharing_a_cached_result_are_published_by_the_owner_finishing(
    git_repo, e2e, publisher
) -> None:
    sha = git_repo.commit({"shop/orders.py": FILES["shop/orders.py"]})
    review_tasks.process_push.delay(push(git_repo, sha))
    owner_unit = unit(e2e, sha)

    # A second unit whose review followed the owner's stream: attached to the same result and
    # completed by the owner's completion, but never triggered by its own review_file task.
    with e2e.sessionmaker() as session:
        owner_review = session.execute(
            select(Review).where(Review.commit_id == owner_unit.id)
        ).scalar_one()
        follower = Commit(
            repo_id=owner_unit.repo_id, sha=sha, ref="refs/heads/release", message="same code"
        )
        session.add(follower)
        session.flush()
        session.add(
            Review(
                repo_id=owner_unit.repo_id,
                commit_id=follower.id,
                file_path="shop/orders.py",
                patch=owner_review.patch,
                result_id=owner_review.result_id,
                cache_hit=True,
                status=ReviewStatus.COMPLETE,
            )
        )
        session.commit()
        follower_id = follower.id

    publish_tasks.schedule_publish_for_review(e2e, owner_review.id)

    assert unit(e2e, sha, "refs/heads/release").publish_status == PublishStatus.PUBLISHED
    assert {t.unit_id for t, _ in publisher.finished} == {owner_unit.id, follower_id}


def test_unit_with_only_skipped_files_publishes_immediately(git_repo, e2e, publisher) -> None:
    sha = git_repo.commit({"package-lock.json": "{}", "logo.png": "not really a png"})
    review_tasks.process_push.delay(push(git_repo, sha))
    [(_, report)] = publisher.finished
    assert report.title == "No issues found"
    assert "skipped: generated lockfile" in report.summary


def test_without_a_github_app_units_are_marked_skipped(git_repo, e2e, monkeypatch) -> None:
    sha = git_repo.commit(FILES)
    review_tasks.process_push.delay(push(git_repo, sha))  # default publisher: local mode → Null
    assert unit(e2e, sha).publish_status == PublishStatus.SKIPPED


@pytest.mark.parametrize(
    ("error", "status"),
    [
        # Retryable: back to pending with the error recorded, and a Celery retry scheduled.
        (SourceError("GitHub 502", retryable=True), PublishStatus.PENDING),
        # Permanent (e.g. missing App permission): recorded as failed, no retry.
        (SourceError("GitHub 403 Resource not accessible", retryable=False), PublishStatus.FAILED),
    ],
)
def test_publish_failures_are_retried_or_recorded(git_repo, e2e, publisher, error, status) -> None:
    sha = git_repo.commit({"shop/orders.py": FILES["shop/orders.py"]})
    publisher.fail_with = error

    with contextlib.suppress(Retry):  # eager Celery raises Retry instead of scheduling it
        review_tasks.process_push.delay(push(git_repo, sha))

    saved = unit(e2e, sha)
    assert saved.publish_status == status
    assert error.args[0] in saved.publish_error
    assert publisher.finished == []


def test_pull_request_units_publish_with_pr_number(git_repo, e2e, publisher) -> None:
    base = git_repo.commit(FILES)
    git_repo.git("checkout", "-q", "-b", "feature")
    head = git_repo.commit({"shop/users.py": "def name(user):\n    return user.full_name\n"})
    review_tasks.process_pull_request.delay(
        {
            "delivery_id": "pr",
            "installation_id": 1,
            "repo": {"github_id": 99, "full_name": git_repo.full_name, "default_branch": "main"},
            "action": "opened",
            "number": 21,
            "title": "Use full names",
            "author": "dev",
            "head_sha": head,
            "head_ref": "feature",
            "head_repo_id": 99,
            "base_sha": base,
            "base_ref": "main",
        }
    )
    [(target, _)] = publisher.finished
    assert (target.kind, target.pr_number, target.sha) == ("pull_request", 21, head)


def test_reviews_still_retrying_block_publishing(git_repo, e2e, publisher) -> None:
    sha = git_repo.commit(FILES)
    review_tasks.process_push.delay(push(git_repo, sha))
    saved = unit(e2e, sha)
    with e2e.sessionmaker() as session:
        session.execute(
            update(Commit).where(Commit.id == saved.id).values(publish_status=PublishStatus.PENDING)
        )
        session.execute(
            update(Review)
            .where(Review.commit_id == saved.id, Review.file_path == "shop/users.py")
            .values(status=ReviewStatus.PENDING)
        )
        session.commit()
    assert publish_tasks.publish_unit.delay(saved.id).get() == "not_ready"
    assert len(publisher.finished) == 1  # only the original publish
