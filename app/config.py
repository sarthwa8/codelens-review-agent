"""Application settings, loaded from environment variables (and `.env` in development)."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- GitHub / source access ---
    # Not validated here so that the worker and migrations can start without it;
    # the API refuses to boot with an empty secret (see app.main.create_app).
    github_webhook_secret: str = ""
    # Fallback auth when no GitHub App is configured (read-only reviews, nothing posted back).
    github_token: str | None = None
    github_api_url: str = "https://api.github.com"
    github_web_url: str = "https://github.com"
    github_api_version: str = "2026-03-10"
    # GitHub App: installation tokens for API access, check runs + PR reviews, and user sign-in.
    github_app_id: str | None = None
    github_app_client_id: str | None = None
    github_app_client_secret: str | None = None
    github_app_slug: str | None = None
    github_app_private_key_path: Path | None = None
    github_app_private_key: str | None = None  # inline PEM alternative to the path
    # Where people reach the dashboard; used for OAuth callbacks and links posted to GitHub.
    public_url: str = "http://localhost:3000"
    # "github" talks to the GitHub REST API; "local" reads bare/working git repos from disk
    # (used by the demo + replay benchmark so the stack can be exercised without GitHub).
    source_mode: Literal["github", "local"] = "github"
    local_repos_dir: Path = Path("/repos")

    # --- Infrastructure ---
    database_url: str = "postgresql+psycopg://codelens:codelens@localhost:5432/codelens"
    redis_url: str = "redis://localhost:6379/0"
    chroma_mode: Literal["http", "ephemeral", "disabled"] = "http"
    chroma_host: str = "localhost"
    chroma_port: int = 8001

    # --- RAG ---
    embedding_provider: Literal["default", "openai", "hashing"] = "default"
    openai_embedding_model: str = "text-embedding-3-small"
    rag_top_k: int = 4
    rag_max_distance: float = 0.65  # cosine distance; larger = less similar
    rag_max_queries: int = 6  # at most this many changed chunks are used as queries

    # --- LLM ---
    llm_provider: Literal["fake", "anthropic", "openai", "ollama", "groq"] = "fake"
    llm_max_output_tokens: int = 2048
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-5"
    openai_api_key: str | None = None
    openai_model: str = "gpt-5-mini"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5-coder:7b"
    groq_api_key: str | None = None
    groq_base_url: str = "https://api.groq.com/openai/v1"
    # gpt-oss-120b is Groq's production code-capable model; the Llama models are Enterprise-only now.
    groq_model: str = "openai/gpt-oss-120b"
    groq_reasoning_effort: Literal["low", "medium", "high"] = "low"
    # Groq's free tier allows 8K tokens/minute and counts the *requested* output budget too, so a
    # single request must stay well below that: ~12K chars of prompt (~4K tokens) + 1.8K output.
    groq_max_prompt_chars: int = 12_000
    groq_max_output_tokens: int = 1_800
    fake_llm_delay_ms: int = 15

    # --- Review pipeline limits ---
    max_file_bytes: int = 300_000
    max_patch_chars: int = 40_000
    max_prompt_chars: int = 60_000
    cache_lease_seconds: int = 300
    task_max_retries: int = 3
    # Rate-limit waits (HTTP 429) are expected on free tiers and get a separate, larger budget.
    rate_limit_max_retries: int = 30

    # --- Streaming ---
    stream_ttl_seconds: int = 3600
    stream_flush_interval_ms: int = 50
    sse_heartbeat_seconds: int = 15

    # --- API ---
    api_token: str | None = None
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    delivery_dedupe_ttl_seconds: int = 86_400

    @property
    def llm_model(self) -> str:
        return {
            "fake": "fake-reviewer-1",
            "anthropic": self.anthropic_model,
            "openai": self.openai_model,
            "ollama": self.ollama_model,
            "groq": self.groq_model,
        }[self.llm_provider]

    @property
    def github_app_configured(self) -> bool:
        has_key = bool(self.github_app_private_key or self.github_app_private_key_path)
        return bool((self.github_app_client_id or self.github_app_id) and has_key)

    def github_app_private_key_pem(self) -> str:
        if self.github_app_private_key:
            # Env files often carry PEMs on one line with literal "\n" escapes.
            return self.github_app_private_key.replace("\\n", "\n")
        if self.github_app_private_key_path:
            return self.github_app_private_key_path.read_text()
        raise RuntimeError("GITHUB_APP_PRIVATE_KEY_PATH or GITHUB_APP_PRIVATE_KEY must be set")

    @property
    def effective_max_prompt_chars(self) -> int:
        if self.llm_provider == "groq":
            return min(self.max_prompt_chars, self.groq_max_prompt_chars)
        return self.max_prompt_chars

    @property
    def effective_max_output_tokens(self) -> int:
        if self.llm_provider == "groq":
            return min(self.llm_max_output_tokens, self.groq_max_output_tokens)
        return self.llm_max_output_tokens


@lru_cache
def get_settings() -> Settings:
    return Settings()
