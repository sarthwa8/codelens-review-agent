import httpx

from app.config import Settings
from app.github.client import GitHubClient
from app.sources.github import GitHubSource
from app.sources.local_git import LocalGitSource


def test_local_pull_request_files_use_merge_base_diff(git_repo) -> None:
    git_repo.commit({"a.py": "def a():\n    return 1\n", "c.py": "C = 1\n"})
    git_repo.git("checkout", "-q", "-b", "feature")
    git_repo.commit({"a.py": "def a():\n    return 2\n"})
    head = git_repo.commit({"b.py": "def b():\n    return 3\n"})
    git_repo.git("checkout", "-q", "main")
    main_now = git_repo.commit({"c.py": "C = 2\n"})  # main moves on after the branch forked

    source = LocalGitSource(git_repo.root)
    files = {
        f.path: f for f in source.get_pull_request_files(git_repo.full_name, 7, main_now, head)
    }

    assert set(files) == {"a.py", "b.py"}, "changes made on main after forking must not appear"
    assert files["a.py"].status == "modified" and files["b.py"].status == "added"
    assert files["a.py"].patch.startswith("@@") and "+    return 2" in files["a.py"].patch
    assert source.find_open_pull_request(git_repo.full_name, "feature") is None


def github_source(routes: dict) -> tuple[GitHubSource, list[httpx.Request]]:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        handler_or_response = routes[request.url.path]
        return (
            handler_or_response(request) if callable(handler_or_response) else handler_or_response
        )

    http = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    return GitHubSource(
        GitHubClient(Settings(_env_file=None, github_token="ghp_x"), http=http)
    ), calls


def test_github_pull_request_files_are_paginated() -> None:
    def files(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        count = 100 if page == 1 else 3
        return httpx.Response(
            200,
            json=[
                {
                    "filename": f"f{page}_{i}.py",
                    "status": "modified",
                    "patch": "@@ -1 +1 @@\n-a\n+b",
                }
                for i in range(count)
            ],
        )

    source, calls = github_source({"/repos/acme/shop/pulls/12/files": files})
    result = source.get_pull_request_files("acme/shop", 12, "base", "head")
    assert len(result) == 103
    assert [c.url.params["page"] for c in calls] == ["1", "2"]


def test_find_open_pull_request_filters_to_same_repo_branch() -> None:
    source, calls = github_source(
        {"/repos/acme/shop/pulls": httpx.Response(200, json=[{"number": 42}])}
    )
    assert source.find_open_pull_request("acme/shop", "feature/x") == 42
    params = calls[0].url.params
    assert (params["head"], params["state"]) == ("acme:feature/x", "open")

    source, _ = github_source({"/repos/acme/shop/pulls": httpx.Response(200, json=[])})
    assert source.find_open_pull_request("acme/shop", "feature/x") is None


def test_github_file_content_handles_missing_and_binary() -> None:
    source, calls = github_source(
        {
            "/repos/acme/shop/contents/src/app.py": httpx.Response(200, content=b"print('hi')\n"),
            "/repos/acme/shop/contents/logo.png": httpx.Response(200, content=b"\x89PNG\x00\x00"),
            "/repos/acme/shop/contents/gone.py": httpx.Response(404),
        }
    )
    assert source.get_file_content("acme/shop", "src/app.py", "abc", 1000) == "print('hi')\n"
    assert source.get_file_content("acme/shop", "logo.png", "abc", 1000) is None
    assert source.get_file_content("acme/shop", "gone.py", "abc", 1000) is None
    assert (
        source.get_file_content("acme/shop", "src/app.py", "abc", 5) is None
    )  # over the size limit
    assert calls[0].headers["accept"] == "application/vnd.github.raw+json"
