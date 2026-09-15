import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.config import Settings
from app.github.client import GitHubClient
from app.sources.base import SourceError


@pytest.fixture(scope="module")
def private_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def app_settings(pem: str) -> Settings:
    return Settings(_env_file=None, github_app_client_id="Iv23test", github_app_private_key=pem)


class Router:
    """Tiny fake GitHub: maps (method, path) to handlers and records every request."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        handler = self.routes[(request.method, request.url.path)]
        return handler(request) if callable(handler) else handler

    def client(self, settings: Settings) -> GitHubClient:
        http = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(self))
        return GitHubClient(settings, http=http, clock=lambda: 1_789_470_000.0)


TOKEN = httpx.Response(201, json={"token": "ghs_install", "expires_at": "2026-09-15T12:00:00Z"})


def test_app_mode_uses_installation_token_for_repo_calls(private_pem: str) -> None:
    router = Router(
        {
            ("GET", "/repos/acme/shop/installation"): httpx.Response(200, json={"id": 42}),
            ("POST", "/app/installations/42/access_tokens"): TOKEN,
            ("GET", "/repos/acme/shop/commits/abc"): httpx.Response(200, json={"files": []}),
        }
    )
    client = router.client(app_settings(private_pem))
    assert client.is_app

    client.request("GET", "/repos/acme/shop/commits/abc", repo="acme/shop")
    client.request("GET", "/repos/acme/shop/commits/abc", repo="acme/shop")

    lookup, _mint, first, second = router.calls
    assert lookup.headers["authorization"].startswith("Bearer ey")  # app JWT
    assert first.headers["authorization"] == "Bearer ghs_install"
    assert second.headers["authorization"] == "Bearer ghs_install"
    assert first.headers["x-github-api-version"] == "2026-03-10"
    assert len(router.calls) == 4  # installation + token looked up once, then cached


def test_webhook_installation_id_skips_lookup(private_pem: str) -> None:
    router = Router(
        {
            ("POST", "/app/installations/9/access_tokens"): TOKEN,
            ("GET", "/repos/acme/shop"): httpx.Response(200, json={}),
        }
    )
    client = router.client(app_settings(private_pem))
    client.remember_installation("Acme/Shop", 9)
    client.request("GET", "/repos/acme/shop", repo="acme/shop")
    assert [c.url.path for c in router.calls] == [
        "/app/installations/9/access_tokens",
        "/repos/acme/shop",
    ]


def test_401_refreshes_the_installation_token_once(private_pem: str) -> None:
    minted = iter(["ghs_old", "ghs_new"])
    responses = iter([httpx.Response(401), httpx.Response(200, json={"ok": True})])
    router = Router(
        {
            ("POST", "/app/installations/9/access_tokens"): lambda r: httpx.Response(
                201, json={"token": next(minted), "expires_at": "2026-09-15T12:00:00Z"}
            ),
            ("GET", "/repos/acme/shop"): lambda r: next(responses),
        }
    )
    client = router.client(app_settings(private_pem))
    client.remember_installation("acme/shop", 9)
    assert client.request("GET", "/repos/acme/shop", repo="acme/shop").status_code == 200
    auth = [c.headers["authorization"] for c in router.calls if c.url.path == "/repos/acme/shop"]
    assert auth == ["Bearer ghs_old", "Bearer ghs_new"]


def test_app_not_installed_is_a_permanent_error(private_pem: str) -> None:
    router = Router({("GET", "/repos/acme/private/installation"): httpx.Response(404)})
    with pytest.raises(SourceError, match="not installed") as caught:
        router.client(app_settings(private_pem)).request(
            "GET", "/repos/acme/private", repo="acme/private"
        )
    assert not caught.value.retryable


@pytest.mark.parametrize(
    ("response", "retry_after"),
    [
        (httpx.Response(429, headers={"retry-after": "30"}), 30.0),
        (
            httpx.Response(
                403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1789470120"}
            ),
            120.0,
        ),
        (httpx.Response(403, text="You have exceeded a secondary rate limit"), 60.0),
    ],
)
def test_rate_limits_are_retryable_with_wait(response: httpx.Response, retry_after: float) -> None:
    router = Router({("GET", "/repos/acme/shop"): response})
    with pytest.raises(SourceError) as caught:
        router.client(Settings(_env_file=None, github_token="ghp_x")).request(
            "GET", "/repos/acme/shop", repo="acme/shop"
        )
    assert caught.value.rate_limited and caught.value.retry_after == retry_after


def test_token_mode_and_plain_403_is_not_a_rate_limit() -> None:
    router = Router(
        {("GET", "/repos/acme/shop"): httpx.Response(403, text="Resource not accessible")}
    )
    client = router.client(Settings(_env_file=None, github_token="ghp_x"))
    assert not client.is_app
    assert client.request("GET", "/repos/acme/shop", repo="acme/shop").status_code == 403
    assert router.calls[0].headers["authorization"] == "Bearer ghp_x"


def test_server_errors_are_retryable() -> None:
    router = Router({("GET", "/repos/acme/shop"): httpx.Response(502)})
    with pytest.raises(SourceError) as caught:
        router.client(Settings(_env_file=None)).request("GET", "/repos/acme/shop", repo="acme/shop")
    assert caught.value.retryable and not caught.value.rate_limited


def test_missing_private_key_file_is_a_clear_permanent_error(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        github_app_client_id="Iv23test",
        github_app_private_key_path=tmp_path / "missing.pem",
    )
    router = Router({})
    with pytest.raises(SourceError, match=r"secrets/github-app\.pem") as caught:
        router.client(settings).request("GET", "/repos/acme/shop", repo="acme/shop")
    assert not caught.value.retryable
    assert router.calls == []
