import logging

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.auth import require_token
from app.api.routes import router as api_router
from app.config import Settings, get_settings
from app.db.session import get_engine
from app.redis_client import get_async_redis
from app.streaming.sse import router as sse_router
from app.webhooks.github import router as webhook_router


def create_app(settings: Settings | None = None) -> FastAPI:
    """App factory (``uvicorn app.main:create_app --factory``)."""
    explicit = settings is not None
    settings = settings or get_settings()
    if not settings.github_webhook_secret:
        # Fail closed: without a secret anyone could trigger (paid) LLM reviews.
        raise RuntimeError("GITHUB_WEBHOOK_SECRET must be set")

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    app = FastAPI(title="CodeLens", version="0.1.0")
    if explicit:
        app.dependency_overrides[get_settings] = lambda: settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET"],
        allow_headers=["Authorization", "Last-Event-ID"],
    )
    app.include_router(webhook_router)
    app.include_router(api_router, prefix="/api", dependencies=[Depends(require_token)])
    app.include_router(sse_router, prefix="/api", dependencies=[Depends(require_token)])

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", tags=["ops"])
    async def readyz() -> JSONResponse:
        checks: dict[str, str] = {}
        try:
            with get_engine().connect() as connection:
                connection.execute(text("SELECT 1"))
            checks["postgres"] = "ok"
        except Exception as exc:
            checks["postgres"] = f"error: {exc.__class__.__name__}"
        try:
            await get_async_redis().ping()
            checks["redis"] = "ok"
        except Exception as exc:
            checks["redis"] = f"error: {exc.__class__.__name__}"
        healthy = all(v == "ok" for v in checks.values())
        return JSONResponse(checks, status_code=200 if healthy else 503)

    return app
