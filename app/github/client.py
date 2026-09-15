"""Authenticated GitHub REST client shared by the source provider and the review publisher.

Credential per request:
* GitHub App configured → an installation token for the repository's installation
  (installation ids come from webhooks, or are looked up once with the app JWT);
* otherwise ``GITHUB_TOKEN`` if set (read-only use), else anonymous.

Every failure is mapped to ``SourceError`` so Celery tasks can decide whether to retry.
"""

import threading
import time
from collections.abc import Callable
from functools import cached_property
from typing import Any

import httpx

from app.config import Settings
from app.github.app_auth import InstallationTokens, make_app_jwt
from app.sources.base import SourceError


class GitHubClient:
    def __init__(
        self,
        settings: Settings,
        http: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self._settings = settings
        self._clock = clock
        self._http = http or httpx.Client(
            base_url=settings.github_api_url, timeout=30, follow_redirects=True
        )
        self._tokens = (
            InstallationTokens(self._http, self.app_jwt, clock)
            if settings.github_app_configured
            else None
        )
        self._installations: dict[str, int] = {}
        self._lock = threading.Lock()

    @property
    def is_app(self) -> bool:
        """Only GitHub Apps may create check runs; posting results back requires one."""
        return self._tokens is not None

    @cached_property
    def _private_key(self) -> str:
        return self._settings.github_app_private_key_pem()

    def app_jwt(self) -> str:
        issuer = self._settings.github_app_client_id or self._settings.github_app_id
        assert issuer, "GitHub App client id or app id is required"
        return make_app_jwt(issuer, self._private_key, now=self._clock())

    def remember_installation(self, full_name: str, installation_id: int | None) -> None:
        if installation_id:
            with self._lock:
                self._installations[full_name.lower()] = int(installation_id)

    def installation_id(self, full_name: str) -> int:
        key = full_name.lower()
        with self._lock:
            if key in self._installations:
                return self._installations[key]
        response = self._send(
            "GET",
            f"/repos/{full_name}/installation",
            headers={"Authorization": f"Bearer {self.app_jwt()}"},
        )
        if response.status_code == 404:
            raise SourceError(f"the GitHub App is not installed on {full_name}")
        self._raise_for_status(response)
        installation_id = int(response.json()["id"])
        self.remember_installation(full_name, installation_id)
        return installation_id

    def _auth_headers(self, repo: str | None) -> dict[str, str]:
        if self._tokens is not None and repo:
            installation_id = self.installation_id(repo)
            try:
                return {"Authorization": f"Bearer {self._tokens.get(installation_id)}"}
            except httpx.HTTPStatusError as exc:
                # e.g. the installation was removed or suspended; retrying won't help.
                raise SourceError(
                    f"cannot mint installation token for {repo}: {exc.response.status_code}"
                ) from exc
            except httpx.TransportError as exc:
                raise SourceError(f"GitHub unreachable: {exc}", retryable=True) from exc
        if self._settings.github_token:
            return {"Authorization": f"Bearer {self._settings.github_token}"}
        return {}

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": self._settings.github_api_version,
            **kwargs.pop("headers", {}),
        }
        try:
            return self._http.request(method, path, headers=headers, **kwargs)
        except httpx.TransportError as exc:
            raise SourceError(f"GitHub unreachable: {exc}", retryable=True) from exc

    def request(self, method: str, path: str, *, repo: str | None, **kwargs: Any) -> httpx.Response:
        """Send a request as the repo's installation. Returns 2xx/4xx responses; raises on 5xx/limits."""
        extra_headers = kwargs.pop("headers", {})
        for attempt in (1, 2):
            response = self._send(
                method, path, headers={**self._auth_headers(repo), **extra_headers}, **kwargs
            )
            if response.status_code == 401 and self._tokens is not None and repo and attempt == 1:
                # Token revoked or expired early: mint a fresh one and try once more.
                self._tokens.invalidate(self.installation_id(repo))
                continue
            self._raise_for_limits(response)
            return response
        return response

    def _raise_for_limits(self, response: httpx.Response) -> None:
        status = response.status_code
        rate_limited = status == 429 or (
            status == 403
            and (
                response.headers.get("x-ratelimit-remaining") == "0"
                or "retry-after" in response.headers
                or "rate limit" in response.text.lower()
            )
        )
        if rate_limited:
            retry_after = float(response.headers.get("retry-after") or 0)
            if not retry_after and response.headers.get("x-ratelimit-reset"):
                retry_after = max(float(response.headers["x-ratelimit-reset"]) - self._clock(), 1.0)
            raise SourceError(
                "GitHub rate limit exceeded",
                retryable=True,
                retry_after=retry_after or 60.0,
                rate_limited=True,
            )
        if status >= 500:
            raise SourceError(f"GitHub {status}", retryable=True)

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_error:
            raise SourceError(f"GitHub {response.status_code}: {response.text[:300]}")

    def json_or_raise(self, response: httpx.Response) -> Any:
        self._raise_for_status(response)
        return response.json()
