import hmac

from fastapi import Depends, HTTPException, Request

from app.config import Settings, get_settings


def require_token(request: Request, settings: Settings = Depends(get_settings)) -> None:
    """Optional shared-token auth for the read API.

    Browsers' EventSource can't send headers, so ``?token=`` is accepted as a fallback. Prefer
    serving the UI same-origin behind a proxy that handles auth for anything internet-facing.
    """
    if not settings.api_token:
        return
    header = request.headers.get("authorization", "")
    token = (
        header[7:].strip()
        if header.lower().startswith("bearer ")
        else request.query_params.get("token")
    )
    if not token or not hmac.compare_digest(token, settings.api_token):
        raise HTTPException(status_code=401, detail="missing or invalid API token")
