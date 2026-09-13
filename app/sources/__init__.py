from app.config import Settings
from app.sources.base import CommitFile, SourceError, SourceProvider


def get_source(settings: Settings) -> SourceProvider:
    if settings.source_mode == "local":
        from app.sources.local_git import LocalGitSource

        return LocalGitSource(settings.local_repos_dir)
    from app.sources.github import GitHubSource

    return GitHubSource(settings.github_token, settings.github_api_url)


__all__ = ["CommitFile", "SourceError", "SourceProvider", "get_source"]
