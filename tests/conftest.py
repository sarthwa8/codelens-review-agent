import os

# Settings are read at import time by some modules, so configure the environment first.
os.environ.setdefault("GITHUB_WEBHOOK_SECRET", "test-secret")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://codelens:codelens@localhost:5432/codelens_test"
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("CHROMA_MODE", "ephemeral")
os.environ.setdefault("EMBEDDING_PROVIDER", "hashing")
os.environ.setdefault("LLM_PROVIDER", "fake")
os.environ.setdefault("FAKE_LLM_DELAY_MS", "0")
os.environ.setdefault("SOURCE_MODE", "local")

import subprocess
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import fakeredis
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.db.session import get_engine, get_sessionmaker
from app.llm.fake_provider import FakeProvider
from app.review.pipeline import PipelineDeps
from app.sources.local_git import LocalGitSource

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def database_url() -> str:
    url = make_url(os.environ["DATABASE_URL"])
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            exists = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": url.database}
            ).scalar()
            if not exists:
                connection.execute(text(f'CREATE DATABASE "{url.database}"'))
    except OperationalError:
        pytest.skip("PostgreSQL is not reachable (run `docker compose up -d postgres redis`)")
    finally:
        admin.dispose()

    # Build the schema with the real migrations, so tests also prove the migrations work.
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url.render_as_string(hide_password=False))
    command.upgrade(config, "head")
    return url.render_as_string(hide_password=False)


@pytest.fixture
def db(database_url: str) -> sessionmaker[Session]:
    with get_engine().begin() as connection:
        connection.execute(
            text("TRUNCATE reviews, review_results, commits, repos RESTART IDENTITY CASCADE")
        )
    return get_sessionmaker()


@pytest.fixture(autouse=True)
def _reset_fake_llm() -> None:
    FakeProvider.reset_calls()


@dataclass
class GitRepo:
    """A throwaway git repository laid out as <root>/<owner>/<name> for LocalGitSource."""

    root: Path
    full_name: str = "acme/shop"
    branch: str = "main"
    shas: list[str] = field(default_factory=list)

    @property
    def path(self) -> Path:
        return self.root / self.full_name

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.path), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    def init(self) -> "GitRepo":
        self.path.mkdir(parents=True)
        self.git("init", "-q", "-b", self.branch)
        self.git("config", "user.email", "dev@example.com")
        self.git("config", "user.name", "Dev")
        return self

    def commit(self, files: dict[str, str | None], message: str = "change") -> str:
        for rel, content in files.items():
            target = self.path / rel
            if content is None:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        sha = self.git("rev-parse", "HEAD")
        self.shas.append(sha)
        return sha


@pytest.fixture
def git_repo(tmp_path: Path) -> GitRepo:
    return GitRepo(root=tmp_path / "repos").init()


@pytest.fixture
def settings(git_repo: GitRepo) -> Settings:
    return get_settings().model_copy(
        update={"local_repos_dir": git_repo.root, "fake_llm_delay_ms": 0}
    )


@pytest.fixture
def sync_redis() -> Iterator[fakeredis.FakeRedis]:
    client = fakeredis.FakeRedis(decode_responses=True)
    yield client
    client.flushall()


@pytest.fixture
def deps(
    db: sessionmaker[Session],
    settings: Settings,
    git_repo: GitRepo,
    sync_redis: fakeredis.FakeRedis,
) -> PipelineDeps:
    return PipelineDeps(
        sessionmaker=db,
        source=LocalGitSource(git_repo.root),
        llm=FakeProvider(delay_ms=0),
        redis=sync_redis,
        settings=settings,
    )
