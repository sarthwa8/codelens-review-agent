import logging

from fastapi import FastAPI

from app.config import Settings, get_settings
from app.webhooks.github import router as webhook_router


def create_app(settings: Settings | None = None) -> FastAPI:
    """App factory (``uvicorn app.main:create_app --factory``)."""
    explicit = settings is not None
    settings = settings or get_settings()
    if not settings.github_webhook_secret:
        # Fail closed: without a secret anyone could trigger (paid) LLM reviews.
        raise RuntimeError("GITHUB_WEBHOOK_SECRET must be set")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = FastAPI(title="CodeLens", version="0.1.0")
    if explicit:
        app.dependency_overrides[get_settings] = lambda: settings
    app.include_router(webhook_router)

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
