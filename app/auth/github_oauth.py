"""GitHub App user authorization (OAuth web flow with PKCE) and user-scoped repo access.

Docs: https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-user-access-token-for-a-github-app
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import Settings

MAX_PAGES = 20  # per_page=100 → up to 2000 installations / repositories per installation


class OAuthError(Exception):
    """GitHub refused the authorization or refresh (e.g. bad_verification_code, bad_refresh_token)."""


@dataclass(frozen=True)
class TokenSet:
    access_token: str
    refresh_token: str | None
    access_expires_at: datetime | None
    refresh_expires_at: datetime | None


class GitHubOAuth:
    def __init__(self, settings: Settings, http: httpx.Client):
        self.settings = settings
        self.http = http

    @property
    def redirect_uri(self) -> str:
        return f"{self.settings.public_url.rstrip('/')}/auth/callback"

    def authorize_url(self, state: str, code_challenge: str) -> str:
        query = urlencode(
            {
                "client_id": self.settings.github_app_client_id,
                "redirect_uri": self.redirect_uri,
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "allow_signup": "false",
            }
        )
        return f"{self.settings.github_web_url}/login/oauth/authorize?{query}"

    def _token_request(self, data: dict[str, str]) -> TokenSet:
        response = self.http.post(
            f"{self.settings.github_web_url}/login/oauth/access_token",
            data={
                "client_id": self.settings.github_app_client_id or "",
                "client_secret": self.settings.github_app_client_secret or "",
                **data,
            },
            headers={"Accept": "application/json"},  # the default response is form-encoded
        )
        body = response.json() if response.content else {}
        if response.is_error or "error" in body or "access_token" not in body:
            raise OAuthError(body.get("error") or f"token endpoint returned {response.status_code}")
        now = datetime.now(UTC)
        expires_in = body.get("expires_in")
        refresh_expires_in = body.get("refresh_token_expires_in")
        return TokenSet(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token"),
            access_expires_at=now + timedelta(seconds=int(expires_in)) if expires_in else None,
            refresh_expires_at=now + timedelta(seconds=int(refresh_expires_in))
            if refresh_expires_in
            else None,
        )

    def exchange_code(self, code: str, code_verifier: str) -> TokenSet:
        return self._token_request(
            {"code": code, "code_verifier": code_verifier, "redirect_uri": self.redirect_uri}
        )

    def refresh(self, refresh_token: str) -> TokenSet:
        # Refresh tokens are single-use: the old access and refresh tokens stop working now.
        return self._token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})

    def _api(self, path: str, token: str, params: dict[str, Any] | None = None) -> Any:
        response = self.http.get(
            f"{self.settings.github_api_url}{path}",
            params=params,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": self.settings.github_api_version,
            },
        )
        if response.status_code == 401:
            raise OAuthError("user token rejected")
        response.raise_for_status()
        return response.json()

    def get_user(self, token: str) -> dict[str, Any]:
        return dict(self._api("/user", token))

    def accessible_repo_ids(self, token: str) -> set[int]:
        """Repos reachable through this App that the user can read: GitHub computes the intersection
        of the user's own access and the App's installations, so no permission logic lives here."""
        repo_ids: set[int] = set()
        for installation_id in self._paged(token, "/user/installations", "installations", "id"):
            repo_ids.update(
                self._paged(
                    token,
                    f"/user/installations/{installation_id}/repositories",
                    "repositories",
                    "id",
                )
            )
        return repo_ids

    def _paged(self, token: str, path: str, key: str, field: str) -> list[int]:
        values: list[int] = []
        for page in range(1, MAX_PAGES + 1):
            items = self._api(path, token, {"per_page": 100, "page": page}).get(key) or []
            values.extend(int(item[field]) for item in items)
            if len(items) < 100:
                break
        return values
