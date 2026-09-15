import json

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.github.app_auth import InstallationTokens, make_app_jwt


@pytest.fixture(scope="module")
def key_pair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private_pem, public_pem


def test_app_jwt_claims_follow_github_rules(key_pair: tuple[str, str]) -> None:
    private_pem, public_pem = key_pair
    token = make_app_jwt("Iv23liClientId", private_pem, now=1_000_000)
    claims = jwt.decode(
        token, public_pem, algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False}
    )
    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    assert claims["iss"] == "Iv23liClientId"
    assert claims["iat"] == 1_000_000 - 60  # backdated for clock drift
    assert claims["exp"] - 1_000_000 <= 600  # GitHub's 10-minute maximum


class FakeGitHub:
    def __init__(self) -> None:
        self.minted = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/app/installations/42/access_tokens"
        assert request.headers["authorization"] == "Bearer app-jwt"
        self.minted += 1
        return httpx.Response(
            201, json={"token": f"ghs_token_{self.minted}", "expires_at": "2026-09-15T12:00:00Z"}
        )


def tokens_with_clock(fake: FakeGitHub, now: list[float]) -> InstallationTokens:
    http = httpx.Client(
        base_url="https://api.github.com", transport=httpx.MockTransport(fake.handler)
    )
    return InstallationTokens(http, jwt_factory=lambda: "app-jwt", clock=lambda: now[0])


EXPIRY = 1_789_473_600.0  # 2026-09-15T12:00:00Z


def test_installation_token_is_cached_until_close_to_expiry() -> None:
    fake = FakeGitHub()
    now = [EXPIRY - 3600]
    tokens = tokens_with_clock(fake, now)

    assert tokens.get(42) == "ghs_token_1"
    now[0] = EXPIRY - 400  # still more than the 5-minute margin left
    assert tokens.get(42) == "ghs_token_1"
    assert fake.minted == 1

    now[0] = EXPIRY - 200  # inside the refresh margin: mint before it can expire mid-review
    assert tokens.get(42) == "ghs_token_2"
    assert fake.minted == 2


def test_invalidate_forces_a_new_token() -> None:
    fake = FakeGitHub()
    tokens = tokens_with_clock(fake, [EXPIRY - 3600])
    tokens.get(42)
    tokens.invalidate(42)
    assert tokens.get(42) == "ghs_token_2"


def test_token_request_failure_raises() -> None:
    http = httpx.Client(
        base_url="https://api.github.com",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(404, content=json.dumps({"message": "Not Found"}))
        ),
    )
    with pytest.raises(httpx.HTTPStatusError):
        InstallationTokens(http, jwt_factory=lambda: "app-jwt").get(7)
