"""Full flow through the real Celery tasks (executed eagerly) and the HTTP API:

push to feature branch → index repo → review files (RAG from the index) → push same commit to
main → cache hits → incremental index update → history + stats + SSE via the API.
"""

from dataclasses import replace

import chromadb
import pytest
from fastapi.testclient import TestClient

from app.celery_app import celery_app
from app.db.models import IndexStatus, Repo, ReviewStatus
from app.llm.fake_provider import FakeProvider
from app.main import create_app
from app.parsing.treesitter import extract_index_chunks
from app.rag.chroma_store import CodeIndex
from app.rag.embeddings import HashingEmbedder
from app.rag.retriever import Retriever
from app.tasks import index_tasks, review_tasks

USERS = """from db import session


def fetch_user(user_id):
    user = session.get(User, user_id)
    if user is None:
        raise NotFound(user_id)
    return user
"""

ORDERS = """from db import session


def fetch_order(order_id):
    return session.get(Order, order_id)
"""

ORDERS_FIXED = ORDERS.replace(
    "    return session.get(Order, order_id)\n",
    "    order = session.get(Order, order_id)\n    if order is None:\n        raise NotFound(order_id)\n    return order\n",
)


@pytest.fixture
def e2e(deps, monkeypatch):
    client = chromadb.EphemeralClient()
    for collection in client.list_collections():
        client.delete_collection(collection.name)
    index = CodeIndex(lambda: client, HashingEmbedder())
    full = replace(
        deps,
        index=index,
        retriever=Retriever(index, top_k=3, max_distance=0.9, max_queries=4),
        settings=deps.settings.model_copy(update={"embedding_provider": "hashing"}),
    )
    monkeypatch.setattr(review_tasks, "get_pipeline_deps", lambda: full)
    monkeypatch.setattr(index_tasks, "get_pipeline_deps", lambda: full)
    monkeypatch.setattr(celery_app.conf, "task_always_eager", True)
    monkeypatch.setattr(celery_app.conf, "task_eager_propagates", True)
    return full


def push_event(git_repo, shas: list[str], ref: str, before: str | None = None) -> dict:
    return {
        "delivery_id": f"{ref}-{shas[-1]}",
        "repo": {"github_id": 99, "full_name": git_repo.full_name, "default_branch": "main"},
        "ref": ref,
        "before": before or "0" * 40,
        "after": shas[-1],
        "forced": False,
        "commits": [
            {"sha": s, "message": git_repo.git("log", "-1", "--format=%s", s), "author": "dev"}
            for s in shas
        ],
    }


def test_push_review_cache_and_api(git_repo, e2e) -> None:
    base = git_repo.commit(
        {"users.py": USERS, "orders.py": ORDERS, "package-lock.json": "{}"}, "initial"
    )
    review_tasks.process_push.delay(push_event(git_repo, [base], "refs/heads/main"))

    with e2e.sessionmaker() as session:
        repo = session.query(Repo).one()
        assert repo.index_status == IndexStatus.READY
        assert repo.indexed_chunks >= 2
    calls_after_initial = FakeProvider.calls
    assert calls_after_initial == 2  # users.py + orders.py; the lockfile is skipped

    # Feature branch fixes orders.py, following the pattern already used in users.py.
    git_repo.git("checkout", "-q", "-b", "feature")
    fix = git_repo.commit({"orders.py": ORDERS_FIXED}, "handle missing orders")
    review_tasks.process_push.delay(push_event(git_repo, [fix], "refs/heads/feature", before=base))
    assert FakeProvider.calls == calls_after_initial + 1

    # Fast-forward merge: the same commit is pushed to main → served entirely from cache.
    git_repo.git("checkout", "-q", "main")
    git_repo.git("merge", "-q", "--ff-only", "feature")
    review_tasks.process_push.delay(push_event(git_repo, [fix], "refs/heads/main", before=base))
    assert FakeProvider.calls == calls_after_initial + 1, "merge push must not call the LLM"

    api = TestClient(create_app(e2e.settings))

    [repo_out] = api.get("/api/repos").json()
    assert repo_out["full_name"] == "acme/shop" and repo_out["index_status"] == "ready"

    commits = api.get("/api/repos/acme/shop/commits").json()["items"]
    assert [(c["ref"], c["sha"]) for c in commits][:2] == [
        ("refs/heads/main", fix),
        ("refs/heads/feature", fix),
    ]

    orders_reviews = api.get(
        "/api/repos/acme/shop/reviews", params={"file_path": "orders.py"}
    ).json()["items"]
    assert [r["cache_hit"] for r in orders_reviews] == [True, False, False]  # newest first
    assert all(r["status"] == ReviewStatus.COMPLETE for r in orders_reviews)

    skipped = api.get("/api/repos/acme/shop/reviews", params={"status": "skipped"}).json()["items"]
    assert [(r["file_path"], r["skip_reason"]) for r in skipped] == [
        ("package-lock.json", "generated lockfile")
    ]

    page = api.get("/api/repos/acme/shop/reviews", params={"limit": 2}).json()
    assert len(page["items"]) == 2 and page["next_before_id"] is not None
    rest = api.get(
        "/api/repos/acme/shop/reviews", params={"limit": 2, "before_id": page["next_before_id"]}
    ).json()
    assert {r["id"] for r in rest["items"]}.isdisjoint({r["id"] for r in page["items"]})

    # The fix was reviewed with RAG context retrieved from *another* file in the same repo.
    detail = api.get(f"/api/reviews/{orders_reviews[1]['id']}").json()
    similar = detail["result"]["context"]["similar_code"]
    assert similar and all(not s["location"].startswith("orders.py") for s in similar)
    assert "### Summary" in detail["review_text"]

    stats = api.get("/api/stats", params={"repo": "acme/shop"}).json()
    assert stats["llm_calls"] == 3 and stats["cache_hits"] == 1
    assert stats["cache_hit_rate"] == 0.25
    assert stats["tokens_saved"] > 0

    with api.stream("GET", f"/api/reviews/{orders_reviews[0]['id']}/stream") as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())
    assert "event: snapshot" in body and '"cache_hit": true' in body and "event: done" in body

    # Incremental index update ran for the main-branch push: fetch_order chunk reflects the fix.
    hits = e2e.index.search(
        repo_out["id"],
        extract_index_chunks("orders.py", ORDERS_FIXED)[0],
        k=5,
        max_distance=2.0,
        exclude_path="users.py",
    )
    assert any("raise NotFound(order_id)" in h.text for h in hits)


def test_api_token_is_enforced(e2e) -> None:
    api = TestClient(create_app(e2e.settings.model_copy(update={"api_token": "s3cret"})))
    assert api.get("/api/repos").status_code == 401
    assert api.get("/api/repos", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert api.get("/api/repos", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert (
        api.get("/api/repos", params={"token": "s3cret"}).status_code == 200
    )  # EventSource fallback
    assert api.get("/healthz").status_code == 200  # health checks stay open


def test_unknown_resources_404(e2e) -> None:
    api = TestClient(create_app(e2e.settings))
    assert api.get("/api/repos/nope/nope/reviews").status_code == 404
    assert api.get("/api/reviews/123456").status_code == 404
    assert api.get("/api/reviews/123456/stream").status_code == 404
