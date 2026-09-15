from typing import TYPE_CHECKING

from app.config import Settings
from app.sources.base import CommitFile, SourceError, SourceProvider

if TYPE_CHECKING:
    from app.github.client import GitHubClient


def get_source(settings: Settings, github: "GitHubClient | None" = None) -> SourceProvider:
    if settings.source_mode == "local":
        from app.sources.local_git import LocalGitSource

        return LocalGitSource(settings.local_repos_dir)
    from app.github.client import GitHubClient
    from app.sources.github import GitHubSource

    return GitHubSource(github or GitHubClient(settings))


__all__ = ["CommitFile", "SourceError", "SourceProvider", "get_source"]
