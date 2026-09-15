"""Sign in with GitHub (GitHub App user authorization, web flow + PKCE)."""

import json
import secrets
from typing import Any

import httpx
import redis
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.auth.crypto import pkce_pair
from app.auth.deps import SESSION_COOKIE, get_oauth_http, get_session_store
from app.auth.github_oauth import GitHubOAuth, OAuthError
from app.auth.sessions import SessionStore
from app.config import Settings, get_settings
from app.redis_client import get_sync_redis

router = APIRouter(prefix="/auth", tags=["auth"])

STATE_TTL_SECONDS = 600  # GitHub's authorization codes expire after 10 minutes


def safe_next(value: str | None) -> str:
    """Only same-site paths: "//evil.com" and absolute URLs would turn login into an open redirect."""
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/"


def _require_github_auth(settings: Settings) -> None:
    if settings.auth_mode != "github":
        raise HTTPException(status_code=404, detail="sign-in is disabled (AUTH_MODE=none)")


@router.get("/login")
def login(
    next: str | None = None,
    settings: Settings = Depends(get_settings),
    cache: redis.Redis = Depends(get_sync_redis),
    http: httpx.Client = Depends(get_oauth_http),
) -> RedirectResponse:
    _require_github_auth(settings)
    state = secrets.token_urlsafe(32)
    verifier, challenge = pkce_pair()
    cache.set(
        f"codelens:oauth:{state}",
        json.dumps({"verifier": verifier, "next": safe_next(next)}),
        ex=STATE_TTL_SECONDS,
    )
    return RedirectResponse(
        GitHubOAuth(settings, http).authorize_url(state, challenge), status_code=302
    )


@router.get("/callback")
def callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    settings: Settings = Depends(get_settings),
    cache: redis.Redis = Depends(get_sync_redis),
    http: httpx.Client = Depends(get_oauth_http),
    store: SessionStore | None = Depends(get_session_store),
) -> RedirectResponse:
    _require_github_auth(settings)
    assert store is not None
    if error:
        raise HTTPException(status_code=400, detail=f"GitHub sign-in was not completed: {error}")
    # GETDEL: a state value works exactly once, which blocks login CSRF and replayed callbacks.
    saved = cache.getdel(f"codelens:oauth:{state}") if state else None
    if not code or not saved:
        raise HTTPException(
            status_code=400, detail="sign-in expired or was tampered with; please try again"
        )
    pending: dict[str, Any] = json.loads(str(saved))

    oauth = GitHubOAuth(settings, http)
    try:
        tokens = oauth.exchange_code(code, pending["verifier"])
        user = oauth.get_user(tokens.access_token)
    except (OAuthError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=400, detail=f"GitHub sign-in failed: {exc}") from exc

    token = store.create(user, tokens)
    response = RedirectResponse(safe_next(pending.get("next")), status_code=302)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_ttl_seconds,
        httponly=True,  # not readable by scripts, so an XSS can't steal the session
        samesite="lax",  # not sent on cross-site POSTs, which covers logout CSRF
        secure=settings.public_url.startswith("https://"),
        path="/",
    )
    return response


@router.post("/logout")
def logout(
    request: Request, store: SessionStore | None = Depends(get_session_store)
) -> RedirectResponse:
    token = request.cookies.get(SESSION_COOKIE)
    if store is not None and token:
        store.delete(token)
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/me")
def me(
    request: Request,
    settings: Settings = Depends(get_settings),
    store: SessionStore | None = Depends(get_session_store),
) -> dict[str, Any]:
    install_url = (
        f"{settings.github_web_url}/apps/{settings.github_app_slug}/installations/new"
        if settings.github_app_slug
        else None
    )
    base = {
        "auth_mode": settings.auth_mode,
        "install_url": install_url,
        "github_web_url": settings.github_web_url,
    }
    if store is None:
        return {**base, "authenticated": True, "login": None, "avatar_url": None}
    token = request.cookies.get(SESSION_COOKIE)
    viewer = store.viewer(token) if token else None
    if viewer is None:
        return {**base, "authenticated": False, "login": None, "avatar_url": None}
    return {**base, "authenticated": True, "login": viewer.login, "avatar_url": viewer.avatar_url}
