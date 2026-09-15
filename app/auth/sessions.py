"""Server-side sessions for signed-in GitHub users, and what each viewer may see."""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import redis
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.auth.crypto import TokenCipher, new_session_token, session_id
from app.auth.github_oauth import GitHubOAuth, OAuthError, TokenSet
from app.config import Settings
from app.db.models import User, UserSession

logger = logging.getLogger(__name__)

REFRESH_MARGIN = timedelta(seconds=60)


@dataclass(frozen=True)
class Viewer:
    login: str | None
    avatar_url: str | None
    # GitHub ids of repositories this viewer may see; None means unrestricted (AUTH_MODE=none).
    repo_ids: frozenset[int] | None

    @classmethod
    def unrestricted(cls) -> "Viewer":
        return cls(login=None, avatar_url=None, repo_ids=None)

    def can_see(self, repo_github_id: int) -> bool:
        return self.repo_ids is None or repo_github_id in self.repo_ids


class SessionStore:
    def __init__(
        self,
        db: sessionmaker[Session],
        settings: Settings,
        oauth: GitHubOAuth,
        cache: redis.Redis,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.db = db
        self.settings = settings
        self.oauth = oauth
        self.cache = cache
        self.clock = clock
        self.cipher = TokenCipher(settings.session_secret)

    def create(self, github_user: dict[str, Any], tokens: TokenSet) -> str:
        token = new_session_token()
        now = self.clock()
        with self.db() as session:
            user_id = session.execute(
                insert(User)
                .values(
                    github_id=int(github_user["id"]),
                    login=github_user["login"],
                    name=github_user.get("name"),
                    avatar_url=github_user.get("avatar_url"),
                )
                .on_conflict_do_update(
                    index_elements=["github_id"],
                    set_={
                        "login": github_user["login"],
                        "name": github_user.get("name"),
                        "avatar_url": github_user.get("avatar_url"),
                        "last_login_at": now,
                    },
                )
                .returning(User.id)
            ).scalar_one()
            session.add(
                UserSession(
                    id=session_id(token),
                    user_id=user_id,
                    **self._token_columns(tokens),
                    expires_at=now + timedelta(seconds=self.settings.session_ttl_seconds),
                )
            )
            session.commit()
        return token

    def _token_columns(self, tokens: TokenSet) -> dict[str, Any]:
        return {
            "access_token_encrypted": self.cipher.encrypt(tokens.access_token),
            "refresh_token_encrypted": self.cipher.encrypt(tokens.refresh_token)
            if tokens.refresh_token
            else None,
            "access_expires_at": tokens.access_expires_at,
            "refresh_expires_at": tokens.refresh_expires_at,
        }

    def delete(self, token: str) -> None:
        with self.db() as session:
            session.execute(delete(UserSession).where(UserSession.id == session_id(token)))
            session.commit()
        self.cache.delete(self._access_key(session_id(token)))

    def revoke_user(self, github_user_id: int) -> int:
        """The user revoked the App's authorization on GitHub: end all their sessions."""
        with self.db() as session:
            ids = (
                session.execute(
                    select(UserSession.id)
                    .join(User, UserSession.user_id == User.id)
                    .where(User.github_id == github_user_id)
                )
                .scalars()
                .all()
            )
            session.execute(delete(UserSession).where(UserSession.id.in_(ids)))
            session.commit()
        for sid in ids:
            self.cache.delete(self._access_key(sid))
        return len(ids)

    def viewer(self, token: str) -> Viewer | None:
        sid = session_id(token)
        with self.db() as session:
            row = session.execute(
                select(UserSession, User)
                .join(User, UserSession.user_id == User.id)
                .where(UserSession.id == sid)
            ).one_or_none()
            if row is None:
                return None
            user_session, user = row
            if user_session.expires_at <= self.clock():
                session.execute(delete(UserSession).where(UserSession.id == sid))
                session.commit()
                return None
            login, avatar = user.login, user.avatar_url

        cached = self.cache.get(self._access_key(sid))
        if cached is not None:
            return Viewer(login, avatar, frozenset(json.loads(str(cached))))

        access_token = self._fresh_access_token(sid)
        if access_token is None:
            return None
        try:
            repo_ids = self.oauth.accessible_repo_ids(access_token)
        except OAuthError:
            self._drop(sid)  # token revoked on GitHub's side
            return None
        except httpx.HTTPError as exc:
            # GitHub unavailable: fail closed for this request, but keep the session.
            logger.warning("could not load repository access for %s: %s", login, exc)
            return Viewer(login, avatar, frozenset())
        self.cache.set(
            self._access_key(sid),
            json.dumps(sorted(repo_ids)),
            ex=self.settings.access_cache_seconds,
        )
        return Viewer(login, avatar, frozenset(repo_ids))

    def _fresh_access_token(self, sid: str) -> str | None:
        with self.db() as session:
            # Row lock: refresh tokens rotate on use, so two API workers must not refresh concurrently.
            user_session = session.execute(
                select(UserSession).where(UserSession.id == sid).with_for_update()
            ).scalar_one_or_none()
            if user_session is None:
                return None
            expires = user_session.access_expires_at
            if expires is None or expires - REFRESH_MARGIN > self.clock():
                session.commit()
                return self.cipher.decrypt(user_session.access_token_encrypted)
            refresh_token = (
                self.cipher.decrypt(user_session.refresh_token_encrypted)
                if user_session.refresh_token_encrypted
                else None
            )
            if refresh_token is None:
                session.execute(delete(UserSession).where(UserSession.id == sid))
                session.commit()
                return None
            try:
                tokens = self.oauth.refresh(refresh_token)
            except (OAuthError, httpx.HTTPError) as exc:
                logger.info("session refresh failed (%s); signing out", exc)
                session.execute(delete(UserSession).where(UserSession.id == sid))
                session.commit()
                return None
            session.execute(
                update(UserSession)
                .where(UserSession.id == sid)
                .values(**self._token_columns(tokens))
            )
            session.commit()
            return tokens.access_token

    def _drop(self, sid: str) -> None:
        with self.db() as session:
            session.execute(delete(UserSession).where(UserSession.id == sid))
            session.commit()
        self.cache.delete(self._access_key(sid))

    @staticmethod
    def _access_key(sid: str) -> str:
        return f"codelens:access:{sid}"
