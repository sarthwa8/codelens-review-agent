import hashlib
import hmac
import json
from typing import Any

import fakeredis
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.redis_client import get_async_redis
from app.webhooks.github import get_enqueuer, verify_signature

SECRET = "test-secret"


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def push_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "ref": "refs/heads/main",
        "before": "a" * 40,
        "after": "b" * 40,
        "repository": {"id": 42, "full_name": "acme/shop", "default_branch": "main"},
        "commits": [
            {
                "id": "b" * 40,
                "message": "fix totals",
                "author": {"username": "dev"},
                "timestamp": "2026-09-14T10:00:00Z",
            }
        ],
    }
    payload.update(overrides)
    return payload


class Recorder:
    def __init__(self, fail: bool = False):
        self.events: list[dict[str, Any]] = []
        self.fail = fail

    def __call__(self, event: dict[str, Any]) -> None:
        if self.fail:
            raise ConnectionError("broker down")
        self.events.append(event)


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


@pytest.fixture
def client(recorder: Recorder) -> TestClient:
    app = create_app(get_settings().model_copy(update={"github_webhook_secret": SECRET}))
    redis = fakeredis.FakeAsyncRedis(decode_responses=True)
    app.dependency_overrides[get_async_redis] = lambda: redis
    app.dependency_overrides[get_enqueuer] = lambda: recorder
    return TestClient(app)


def post(
    client: TestClient,
    payload: dict[str, Any] | bytes,
    *,
    event: str = "push",
    delivery: str = "d-1",
    signature: str | None = None,
):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    headers = {
        "X-GitHub-Event": event,
        "X-GitHub-Delivery": delivery,
        "Content-Type": "application/json",
    }
    headers["X-Hub-Signature-256"] = signature if signature is not None else sign(body)
    return client.post("/webhooks/github", content=body, headers=headers)


def test_verify_signature_uses_raw_bytes() -> None:
    body = b'{"a": 1}'
    assert verify_signature(SECRET, body, sign(body))
    assert not verify_signature(SECRET, b'{"a":1}', sign(body))  # re-serialised JSON must fail
    assert not verify_signature(SECRET, body, sign(body, "other-secret"))
    assert not verify_signature(SECRET, body, None)
    assert not verify_signature(SECRET, body, "sha1=" + "0" * 40)
    assert not verify_signature("", body, sign(body, ""))  # an empty secret never validates


def test_valid_push_is_accepted_and_enqueued(client: TestClient, recorder: Recorder) -> None:
    response = post(client, push_payload())
    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    [event] = recorder.events
    assert event["repo"] == {"github_id": 42, "full_name": "acme/shop", "default_branch": "main"}
    assert event["commits"][0]["sha"] == "b" * 40
    assert event["delivery_id"] == "d-1"


@pytest.mark.parametrize("signature", ["", "sha256=deadbeef", sign(b"something else")])
def test_bad_signature_is_rejected(client: TestClient, recorder: Recorder, signature: str) -> None:
    response = post(client, push_payload(), signature=signature)
    assert response.status_code == 401
    assert recorder.events == []


def test_ping_returns_pong(client: TestClient) -> None:
    response = post(client, {"zen": "hi"}, event="ping")
    assert response.status_code == 200
    assert response.json() == {"status": "pong"}


def test_redelivery_is_deduplicated(client: TestClient, recorder: Recorder) -> None:
    assert post(client, push_payload(), delivery="same").json()["status"] == "accepted"
    assert post(client, push_payload(), delivery="same").json()["status"] == "duplicate"
    assert len(recorder.events) == 1


@pytest.mark.parametrize(
    ("payload", "event", "reason"),
    [
        (push_payload(deleted=True, after="0" * 40), "push", "branch deletion"),
        (push_payload(ref="refs/tags/v1.0"), "push", "not a branch push"),
        (push_payload(commits=[], head_commit=None), "push", "push contains no commits"),
        (push_payload(), "issues", "event 'issues' is not handled"),
    ],
)
def test_irrelevant_events_are_ignored(
    client: TestClient, recorder: Recorder, payload: dict, event: str, reason: str
) -> None:
    response = post(client, payload, event=event)
    assert response.status_code == 202
    assert response.json() == {"status": "ignored", "reason": reason}
    assert recorder.events == []


def test_new_branch_push_falls_back_to_head_commit(client: TestClient, recorder: Recorder) -> None:
    head = {"id": "c" * 40, "message": "existing commit", "author": {"name": "Dev"}}
    assert post(client, push_payload(commits=[], head_commit=head)).status_code == 202
    assert recorder.events[0]["commits"][0]["sha"] == "c" * 40


def test_invalid_json_is_400(client: TestClient) -> None:
    assert post(client, b"{not json").status_code == 400


def test_enqueue_failure_returns_503_and_releases_dedupe_key(recorder: Recorder) -> None:
    app = create_app(get_settings().model_copy(update={"github_webhook_secret": SECRET}))
    redis = fakeredis.FakeAsyncRedis(decode_responses=True)
    failing = Recorder(fail=True)
    app.dependency_overrides[get_async_redis] = lambda: redis
    app.dependency_overrides[get_enqueuer] = lambda: failing
    client = TestClient(app)
    assert post(client, push_payload(), delivery="retry-me").status_code == 503

    app.dependency_overrides[get_enqueuer] = lambda: recorder
    assert post(client, push_payload(), delivery="retry-me").json()["status"] == "accepted"


def test_app_refuses_to_start_without_secret() -> None:
    with pytest.raises(RuntimeError, match="GITHUB_WEBHOOK_SECRET"):
        create_app(get_settings().model_copy(update={"github_webhook_secret": ""}))
