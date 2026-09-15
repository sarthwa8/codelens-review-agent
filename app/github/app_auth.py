"""GitHub App authentication: app JWTs and cached installation access tokens.

Docs: https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx
import jwt

# GitHub rejects JWTs expiring more than 10 minutes ahead of *its* clock; backdating iat and
# shortening exp tolerates a slightly skewed local clock.
JWT_BACKDATE_SECONDS = 60
JWT_LIFETIME_SECONDS = 540
# Refresh installation tokens (valid 1 hour) this long before they expire, so a token never
# expires in the middle of a review.
TOKEN_REFRESH_MARGIN_SECONDS = 300


def make_app_jwt(issuer: str, private_key_pem: str, now: float | None = None) -> str:
    """``issuer`` is the App's client ID (recommended by GitHub) or its numeric App ID."""
    issued = int(now if now is not None else time.time())
    payload = {
        "iat": issued - JWT_BACKDATE_SECONDS,
        "exp": issued + JWT_LIFETIME_SECONDS,
        "iss": issuer,
    }
    return jwt.encode(payload, private_key_pem, algorithm="RS256")


@dataclass
class _CachedToken:
    token: str
    expires_at: float


class InstallationTokens:
    """Per-process cache of installation tokens. Tokens are never persisted or put in task messages."""

    def __init__(
        self,
        http: httpx.Client,
        jwt_factory: Callable[[], str],
        clock: Callable[[], float] = time.time,
    ):
        self._http = http
        self._jwt_factory = jwt_factory
        self._clock = clock
        self._tokens: dict[int, _CachedToken] = {}
        self._lock = threading.Lock()

    def get(self, installation_id: int) -> str:
        with self._lock:
            cached = self._tokens.get(installation_id)
            if cached and cached.expires_at - TOKEN_REFRESH_MARGIN_SECONDS > self._clock():
                return cached.token
            response = self._http.post(
                f"/app/installations/{installation_id}/access_tokens",
                headers={"Authorization": f"Bearer {self._jwt_factory()}"},
            )
            response.raise_for_status()
            body = response.json()
            expires_at = _parse_github_time(body["expires_at"])
            self._tokens[installation_id] = _CachedToken(body["token"], expires_at)
            return str(body["token"])

    def invalidate(self, installation_id: int) -> None:
        with self._lock:
            self._tokens.pop(installation_id, None)


def _parse_github_time(value: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
