"""Sign in with GitHub, sessions, and repository-scoped access across the API and SSE."""

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import fakeredis
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.auth.crypto import session_id
from app.auth.deps import SESSION_COOKIE, get_oauth_http
from app.auth.routes import safe_next
from app.config import get_settings
from app.db.models import Commit, Repo, Review, User, UserSession
from app.main import create_app
from app.redis_client import get_sync_redis

SECRET = "s" * 40


class FakeGitHub:
    """github.com OAuth endpoints + api.github.com user/installations, for one user."""

    def __init__(self, visible_repo_ids: list[int]):
        self.visible_repo_ids = visible_repo_ids
        self.token_requests: list[dict] = []
        self.api_tokens: list[str] = []
        self.issued = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "github.com" and request.url.path == "/login/oauth/access_token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_requests.append(form)
            if form.get("code") == "bad":
                return httpx.Response(200, json={"error": "bad_verification_code"})
            self.issued += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"ghu_access_{self.issued}",
                    "refresh_token": f"ghr_refresh_{self.issued}",
                    "expires_in": 28800,
                    "refresh_token_expires_in": 15897600,
                    "token_type": "bearer",
                },
            )
        self.api_tokens.append(request.headers["authorization"])
        path = request.url.path
        if path == "/user":
            return httpx.Response(
                200,
                json={
                    "id": 4242,
                    "login": "octodev",
                    "name": "Octo Dev",
                    "avatar_url": "https://avatars/x",
                },
            )
        if path == "/user/installations":
            return httpx.Response(200, json={"total_count": 1, "installations": [{"id": 77}]})
        if path == "/user/installations/77/repositories":
            return httpx.Response(
                200, json={"repositories": [{"id": i} for i in self.visible_repo_ids]}
            )
        raise AssertionError(f"unexpected {request.method} {request.url}")


@pytest.fixture
def github() -> FakeGitHub:
    return FakeGitHub(visible_repo_ids=[1001])


@pytest.fixture
def api(db, github) -> TestClient:
    settings = get_settings().model_copy(
        update={
            "auth_mode": "github",
            "github_app_client_id": "Iv23client",
            "github_app_client_secret": "shh",
            "session_secret": SECRET,
            "public_url": "https://codelens.example.com",
        }
    )
    app = create_app(settings)
    cache = fakeredis.FakeRedis(decode_responses=True)
    app.dependency_overrides[get_sync_redis] = lambda: cache
    app.dependency_overrides[get_oauth_http] = lambda: httpx.Client(
        transport=httpx.MockTransport(github)
    )
    client = TestClient(app, base_url="https://codelens.example.com", follow_redirects=False)
    client.cache = cache  # type: ignore[attr-defined]
    return client


@pytest.fixture
def repos(db) -> dict[str, int]:
    """One repository the user can read on GitHub (1001) and one they can't (2002), each with a review."""
    ids = {}
    with db() as session:
        for github_id, name in [(1001, "acme/visible"), (2002, "acme/secret")]:
            repo = Repo(github_id=github_id, full_name=name)
            session.add(repo)
            session.flush()
            commit = Commit(repo_id=repo.id, sha="a" * 40, ref="refs/heads/main", message="m")
            session.add(commit)
            session.flush()
            review = Review(
                repo_id=repo.id,
                commit_id=commit.id,
                file_path="a.py",
                status="skipped",
                skip_reason="x",
            )
            session.add(review)
            session.flush()
            ids[name] = review.id
        session.commit()
    return ids


def sign_in(api: TestClient, next_path: str = "/repos/acme/visible") -> httpx.Response:
    login = api.get("/auth/login", params={"next": next_path})
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    return api.get("/auth/callback", params={"code": "good", "state": state})


def test_login_redirects_to_github_with_pkce_and_single_use_state(api) -> None:
    response = api.get("/auth/login", params={"next": "/repos/acme/visible"})
    assert response.status_code == 302
    location = urlparse(response.headers["location"])
    query = {k: v[0] for k, v in parse_qs(location.query).items()}
    assert (location.netloc, location.path) == ("github.com", "/login/oauth/authorize")
    assert query["client_id"] == "Iv23client"
    assert query["redirect_uri"] == "https://codelens.example.com/auth/callback"
    assert query["code_challenge_method"] == "S256" and len(query["code_challenge"]) >= 43
    saved = json.loads(api.cache.get(f"codelens:oauth:{query['state']}"))
    assert saved["next"] == "/repos/acme/visible"


def test_callback_creates_session_with_secure_cookie_and_encrypted_tokens(api, github, db) -> None:
    response = sign_in(api)
    assert response.status_code == 302 and response.headers["location"] == "/repos/acme/visible"
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Secure" in cookie
    token = response.cookies[SESSION_COOKIE]

    [exchange] = github.token_requests
    assert exchange["code"] == "good" and len(exchange["code_verifier"]) >= 43  # PKCE verifier sent

    with db() as session:
        stored = session.execute(select(UserSession)).scalar_one()
        assert stored.id == session_id(token) and token not in stored.id
        assert "ghu_access" not in stored.access_token_encrypted
        assert session.execute(select(User.login)).scalar_one() == "octodev"


def test_state_is_single_use_and_forged_states_fail(api) -> None:
    login = api.get("/auth/login")
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    assert api.get("/auth/callback", params={"code": "good", "state": state}).status_code == 302
    assert (
        api.get("/auth/callback", params={"code": "good", "state": state}).status_code == 400
    )  # replay
    assert api.get("/auth/callback", params={"code": "good", "state": "forged"}).status_code == 400


def test_github_rejecting_the_code_is_a_clean_400(api) -> None:
    login = api.get("/auth/login")
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    response = api.get("/auth/callback", params={"code": "bad", "state": state})
    assert response.status_code == 400 and "bad_verification_code" in response.json()["detail"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/repos/a/b?x=1", "/repos/a/b?x=1"),
        ("//evil.com", "/"),
        ("https://evil.com", "/"),
        ("/\\evil.com", "/"),
        (None, "/"),
    ],
)
def test_next_parameter_cannot_redirect_off_site(value, expected) -> None:
    assert safe_next(value) == expected


def test_api_requires_sign_in_and_only_shows_repos_the_user_can_read(api, repos) -> None:
    assert api.get("/api/repos").status_code == 401
    assert api.get("/auth/me").json()["authenticated"] is False
    sign_in(api)

    assert [r["full_name"] for r in api.get("/api/repos").json()] == ["acme/visible"]
    assert api.get("/api/repos/acme/visible/commits").status_code == 200
    # Hidden repositories look exactly like missing ones.
    assert api.get("/api/repos/acme/secret").status_code == 404
    assert api.get("/api/repos/acme/secret/reviews").status_code == 404
    assert api.get(f"/api/reviews/{repos['acme/secret']}").status_code == 404
    assert api.get(f"/api/reviews/{repos['acme/secret']}/stream").status_code == 404
    assert api.get(f"/api/reviews/{repos['acme/visible']}").status_code == 200
    assert api.get("/api/stats").json()["reviews_total"] == 1
    assert api.get("/api/stats", params={"repo": "acme/secret"}).status_code == 404
    me = api.get("/auth/me").json()
    assert (me["authenticated"], me["login"]) == (True, "octodev")


def test_repo_access_is_cached_then_refetched(api, github, repos) -> None:
    sign_in(api)
    api.get("/api/repos")
    api.get("/api/repos")
    # /user at sign-in, then /user/installations + its repositories once; the second request is cached.
    assert github.api_tokens == ["Bearer ghu_access_1"] * 3
    api.cache.flushall()
    github.visible_repo_ids = [1001, 2002]  # access granted on GitHub
    assert {r["full_name"] for r in api.get("/api/repos").json()} == {"acme/visible", "acme/secret"}


def test_expired_access_token_is_refreshed_and_rotated(api, github, db, repos) -> None:
    token = sign_in(api).cookies[SESSION_COOKIE]
    with db() as session:
        session.execute(
            update(UserSession).values(access_expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        session.commit()
    api.cache.flushall()

    assert api.get("/api/repos").status_code == 200
    refresh = github.token_requests[-1]
    assert (refresh["grant_type"], refresh["refresh_token"]) == ("refresh_token", "ghr_refresh_1")
    assert github.api_tokens[-1] == "Bearer ghu_access_2"  # the rotated token is used
    with db() as session:
        stored = session.get(UserSession, session_id(token))
        assert stored.access_expires_at > datetime.now(UTC)


def test_logout_ends_the_session(api, repos, db) -> None:
    sign_in(api)
    response = api.post("/auth/logout")
    assert response.status_code == 303
    api.cookies.clear()
    assert api.get("/api/repos").status_code == 401
    with db() as session:
        assert session.execute(select(UserSession)).first() is None


def test_revoked_user_sessions_are_deleted(api, db, repos) -> None:
    from app.auth.github_oauth import GitHubOAuth
    from app.auth.sessions import SessionStore
    from app.db.session import get_sessionmaker

    token = sign_in(api).cookies[SESSION_COOKIE]
    settings = api.app.dependency_overrides[get_settings]()
    store = SessionStore(
        get_sessionmaker(), settings, GitHubOAuth(settings, httpx.Client()), api.cache
    )
    assert store.revoke_user(4242) == 1
    assert store.viewer(token) is None


def test_auth_mode_github_refuses_to_start_without_credentials() -> None:
    settings = get_settings().model_copy(update={"auth_mode": "github", "session_secret": "short"})
    with pytest.raises(RuntimeError, match=r"GITHUB_APP_CLIENT_ID.*SESSION_SECRET"):
        create_app(settings)


def test_auth_mode_none_keeps_local_development_open(db, repos) -> None:
    api = TestClient(create_app(get_settings()))
    assert api.get("/api/repos").status_code == 200
    assert api.get("/auth/me").json()["auth_mode"] == "none"
    assert api.get("/auth/login").status_code == 404
