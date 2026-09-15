import base64
import hashlib

import pytest

from app.auth.crypto import TokenCipher, new_session_token, pkce_pair, session_id


def test_session_ids_are_hashes_of_the_cookie_token() -> None:
    token = new_session_token()
    assert session_id(token) == hashlib.sha256(token.encode()).hexdigest()
    assert token not in session_id(token)
    assert new_session_token() != token


def test_tokens_round_trip_encrypted_and_fail_closed_on_secret_rotation() -> None:
    cipher = TokenCipher("a" * 40)
    encrypted = cipher.encrypt("ghu_secret_user_token")
    assert "ghu_secret" not in encrypted
    assert cipher.decrypt(encrypted) == "ghu_secret_user_token"
    assert TokenCipher("b" * 40).decrypt(encrypted) is None


def test_short_secrets_are_rejected() -> None:
    with pytest.raises(ValueError, match="32 characters"):
        TokenCipher("short")


def test_pkce_challenge_is_s256_of_verifier() -> None:
    verifier, challenge = pkce_pair()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    assert challenge == expected
    assert 43 <= len(verifier) <= 128  # RFC 7636 bounds
