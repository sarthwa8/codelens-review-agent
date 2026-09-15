"""Pull request review units and the "review the PR instead of its pushes" policy."""

from dataclasses import replace

import pytest
from sqlalchemy import select

from app.db.models import Commit, PublishStatus, Repo, Review, UnitKind
from app.llm.fake_provider import FakeProvider
from app.sources.local_git import LocalGitSource
from app.tasks import review_tasks

BASE = {
    "shop/orders.py": "def total(items):\n    return sum(i.price for i in items)\n",
    "README.md": "# Shop\n",
}


def pr_event(git_repo, *, number: int, base_sha: str, head_sha: str, delivery: str = "d1") -> dict:
    return {
        "delivery_id": delivery,
        "installation_id": 555,
        "repo": {"github_id": 99, "full_name": git_repo.full_name, "default_branch": "main"},
        "action": "opened",
        "number": number,
        "title": "Add discounts",
        "author": "dev",
        "head_sha": head_sha,
        "head_ref": "feature",
        "head_repo_id": 99,
        "base_sha": base_sha,
        "base_ref": "main",
    }


def push_event(git_repo, sha: str, ref: str) -> dict:
    return {
        "delivery_id": f"push-{sha}",
        "installation_id": None,
        "repo": {"github_id": 99, "full_name": git_repo.full_name, "default_branch": "main"},
        "ref": ref,
        "before": "0" * 40,
        "after": sha,
        "forced": False,
        "commits": [{"sha": sha, "message": "change", "author": "dev"}],
    }


class SourceWithOpenPR(LocalGitSource):
    def __init__(self, root, open_pr: int | None):
        super().__init__(root)
        self.open_pr = open_pr
        self.lookups: list[tuple[str, str]] = []

    def find_open_pull_request(self, full_name: str, branch: str) -> int | None:
        self.lookups.append((full_name, branch))
        return self.open_pr


def test_pull_request_unit_reviews_exactly_the_pr_diff(git_repo, e2e) -> None:
    base = git_repo.commit(BASE)
    git_repo.git("checkout", "-q", "-b", "feature")
    git_repo.commit(
        {"shop/orders.py": BASE["shop/orders.py"].replace("i.price", "i.price * i.qty")}
    )
    head = git_repo.commit(
        {"shop/discounts.py": "def apply(total, pct):\n    return total * (100 - pct) / 100\n"}
    )

    review_tasks.process_pull_request.delay(
        pr_event(git_repo, number=7, base_sha=base, head_sha=head)
    )

    with e2e.sessionmaker() as session:
        unit = session.execute(
            select(Commit).where(Commit.kind == UnitKind.PULL_REQUEST)
        ).scalar_one()
        assert (unit.ref, unit.sha, unit.pr_number, unit.base_sha) == (
            "refs/pull/7/head",
            head,
            7,
            base,
        )
        assert unit.message == "Add discounts"
        files = (
            session.execute(select(Review.file_path).where(Review.commit_id == unit.id))
            .scalars()
            .all()
        )
        assert sorted(files) == [
            "shop/discounts.py",
            "shop/orders.py",
        ]  # README untouched by the PR
        assert session.execute(select(Repo.installation_id)).scalar_one() == 555
    assert FakeProvider.calls == 2


def test_redelivered_pull_request_event_does_not_review_again(git_repo, e2e) -> None:
    base = git_repo.commit(BASE)
    git_repo.git("checkout", "-q", "-b", "feature")
    head = git_repo.commit({"shop/orders.py": "def total(items):\n    return 0\n"})
    event = pr_event(git_repo, number=3, base_sha=base, head_sha=head)

    review_tasks.process_pull_request.delay(event)
    review_tasks.process_pull_request.delay(
        {**event, "delivery_id": "another-delivery", "action": "reopened"}
    )

    with e2e.sessionmaker() as session:
        assert len(session.execute(select(Commit.id)).all()) == 1
        assert len(session.execute(select(Review.id)).all()) == 1
    assert FakeProvider.calls == 1


def test_push_to_branch_with_open_pr_is_recorded_but_not_reviewed(
    git_repo, e2e, monkeypatch
) -> None:
    git_repo.commit(BASE)
    git_repo.git("checkout", "-q", "-b", "feature")
    sha = git_repo.commit({"shop/orders.py": "def total(items):\n    return 1\n"})
    source = SourceWithOpenPR(git_repo.root, open_pr=12)
    deps = replace(e2e, source=source)
    monkeypatch.setattr(review_tasks, "get_pipeline_deps", lambda: deps)

    result = review_tasks.process_push.delay(push_event(git_repo, sha, "refs/heads/feature")).get()

    assert result["skipped_for_pull_request"] == 12
    assert source.lookups == [(git_repo.full_name, "feature")]
    with deps.sessionmaker() as session:
        commit = session.execute(select(Commit)).scalar_one()
        assert commit.skip_reason == "reviewed in pull request #12"
        assert commit.publish_status == PublishStatus.SKIPPED
        assert session.execute(select(Review.id)).first() is None
    assert FakeProvider.calls == 0


@pytest.mark.parametrize(("open_pr", "opt_in"), [(None, False), (12, True)])
def test_push_is_reviewed_without_open_pr_or_when_opted_in(
    git_repo, e2e, monkeypatch, open_pr, opt_in
) -> None:
    sha = git_repo.commit(BASE)
    settings = e2e.settings.model_copy(update={"review_pushes_with_open_pr": opt_in})
    deps = replace(e2e, source=SourceWithOpenPR(git_repo.root, open_pr), settings=settings)
    monkeypatch.setattr(review_tasks, "get_pipeline_deps", lambda: deps)

    result = review_tasks.process_push.delay(push_event(git_repo, sha, "refs/heads/main")).get()

    assert result["skipped_for_pull_request"] is None
    assert FakeProvider.calls == 2  # both files reviewed
