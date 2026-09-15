"""FastAPI dependencies: who is viewing, and the session store behind it."""

from functools import lru_cache

import httpx
import redis
from fastapi import Depends, HTTPException, Request

from app.auth.github_oauth import GitHubOAuth
from app.auth.sessions import SessionStore, Viewer
from app.config import Settings, get_settings
from app.db.session import get_sessionmaker
from app.redis_client import get_sync_redis

SESSION_COOKIE = "codelens_session"


@lru_cache
def get_oauth_http() -> httpx.Client:
    return httpx.Client(timeout=15)


def get_session_store(
    settings: Settings = Depends(get_settings),
    cache: redis.Redis = Depends(get_sync_redis),
    http: httpx.Client = Depends(get_oauth_http),
) -> SessionStore | None:
    if settings.auth_mode == "none":
        return None
    return SessionStore(get_sessionmaker(), settings, GitHubOAuth(settings, http), cache)


def get_viewer(request: Request, store: SessionStore | None = Depends(get_session_store)) -> Viewer:
    if store is None:
        return Viewer.unrestricted()
    token = request.cookies.get(SESSION_COOKIE)
    viewer = store.viewer(token) if token else None
    if viewer is None:
        raise HTTPException(status_code=401, detail="sign in with GitHub")
    return viewer
