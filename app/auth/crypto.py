"""Secrets at rest for sign-in: hashed session ids and encrypted GitHub user tokens."""

import base64
import hashlib
import secrets

from cryptography.fernet import Fernet, InvalidToken


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def session_id(token: str) -> str:
    """What the database stores: a hash, so a leaked table can't be replayed as cookies."""
    return hashlib.sha256(token.encode()).hexdigest()


class TokenCipher:
    def __init__(self, secret: str):
        if len(secret) < 32:
            raise ValueError("SESSION_SECRET must be at least 32 characters")
        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
        self._fernet = Fernet(key)

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str | None:
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except InvalidToken:
            return None  # e.g. SESSION_SECRET was rotated: treat as signed out


def pkce_pair() -> tuple[str, str]:
    """(code_verifier, code_challenge) for OAuth PKCE with S256."""
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    return verifier, challenge
