#!/usr/bin/env python3
"""Replay a realistic git workflow through the running stack and measure the cache hit rate.

The spec's "~40% fewer LLM calls" can't be guaranteed by code — it depends entirely on how a team
pushes. This script replays common workflows that produce duplicate content (fast-forward merges,
cherry-picks, revert + re-apply, amend + force-push, rebases) alongside genuinely new work, waits
for each push to finish, and reports LLM calls vs. cache hits per step from the /api/stats endpoint.

Requires the stack running with SOURCE_MODE=local (repos are read from ./.demo-repos):

    SOURCE_MODE=local docker compose up -d --build
    python scripts/replay_benchmark.py
"""

import argparse
import os
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from webhook_payload import ZERO_SHA, build_push, git, signed_request

ROOT = Path(__file__).resolve().parent.parent

FILES = {
    "shop/orders.py": """from decimal import Decimal

from shop.db import session
from shop.errors import NotFound


def get_order(order_id: int):
    order = session.get("orders", order_id)
    if order is None:
        raise NotFound(f"order {order_id}")
    return order


def order_total(order) -> Decimal:
    return sum((line.price * line.quantity for line in order.lines), Decimal("0"))
""",
    "shop/users.py": """from shop.db import session
from shop.errors import NotFound


def get_user(user_id: int):
    user = session.get("users", user_id)
    if user is None:
        raise NotFound(f"user {user_id}")
    return user


def display_name(user) -> str:
    return user.nickname or user.email.split("@")[0]
""",
    "shop/payments.py": """import logging

from shop.gateway import client

log = logging.getLogger(__name__)


def charge(order, card_token: str) -> str:
    response = client.charge(amount=order.total, token=card_token)
    log.info("charged order %s", order.id)
    return response["id"]
""",
    "shop/db.py": """class Session:
    def __init__(self):
        self._tables = {}

    def get(self, table: str, key):
        return self._tables.get(table, {}).get(key)


session = Session()
""",
    "shop/errors.py": '''class NotFound(Exception):
    """Raised when a requested entity does not exist."""
''',
    "web/src/pricing.ts": """export interface Line {
  price: number;
  quantity: number;
}

export function subtotal(lines: Line[]): number {
  return lines.reduce((sum, line) => sum + line.price * line.quantity, 0);
}
""",
    "README.md": "# Storefront\n\nDemo repository for the CodeLens replay benchmark.\n",
}


class Bench:
    def __init__(self, api: str, secret: str, full_name: str, repos_dir: Path):
        self.api = api.rstrip("/")
        self.secret = secret
        self.full_name = full_name
        self.repo = repos_dir / full_name
        self.http = httpx.Client(timeout=15)
        self.rows: list[tuple[str, int, int]] = []

    # --- git helpers ---
    def git(self, *args: str) -> str:
        return git(self.repo, *args)

    def write(self, files: dict[str, str]) -> None:
        for rel, content in files.items():
            path = self.repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    def commit(self, message: str, files: dict[str, str] | None = None) -> str:
        if files:
            self.write(files)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def edit(self, rel: str, old: str, new: str) -> dict[str, str]:
        content = (self.repo / rel).read_text()
        assert old in content, f"{old!r} not in {rel}"
        return {rel: content.replace(old, new, 1)}

    # --- stack helpers ---
    def stats(self) -> dict:
        response = self.http.get(f"{self.api}/api/stats", params={"repo": self.full_name})
        if response.status_code == 404:
            return {"llm_calls": 0, "cache_hits": 0}
        response.raise_for_status()
        return response.json()

    def push(self, label: str, branch: str, before: str, after: str, forced: bool = False) -> None:
        start = self.stats()
        payload = build_push(
            self.repo, self.full_name, f"refs/heads/{branch}", before, after, forced=forced
        )
        body, headers = signed_request(payload, self.secret)
        response = self.http.post(f"{self.api}/webhooks/github", content=body, headers=headers)
        response.raise_for_status()
        self.wait_for(branch, [c["id"] for c in payload["commits"]])
        end = self.stats()
        calls, hits = end["llm_calls"] - start["llm_calls"], end["cache_hits"] - start["cache_hits"]
        self.rows.append((label, calls, hits))
        print(f"  {label:<58} LLM calls {calls:>2}   cache hits {hits:>2}")

    def wait_for(self, branch: str, shas: list[str], timeout: float = 180) -> None:
        deadline = time.time() + timeout
        ref = f"refs/heads/{branch}"
        while time.time() < deadline:
            time.sleep(0.5)
            response = self.http.get(
                f"{self.api}/api/repos/{self.full_name}/commits", params={"limit": 100}
            )
            if response.status_code == 404:
                continue
            commits = {(c["sha"], c["ref"]): c for c in response.json()["items"]}
            pushed = [commits.get((sha, ref)) for sha in shas]
            if all(c and c["reviews"] for c in pushed) and not any(
                r["status"] in ("pending", "streaming") for c in pushed if c for r in c["reviews"]
            ):
                return
        raise TimeoutError(
            f"push to {branch} did not finish within {timeout}s — is the worker running?"
        )

    # --- scenario ---
    def run(self) -> None:
        if self.repo.exists():
            shutil.rmtree(self.repo)
        self.repo.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "dev@storefront.test")
        self.git("config", "user.name", "Storefront Dev")

        print(f"\nReplaying workflow for {self.full_name}\n")
        base = self.commit("Initial storefront", FILES)
        self.push("1. initial import (7 files)", "main", ZERO_SHA, base)

        self.git("checkout", "-q", "-b", "feature/discounts")
        self.commit(
            "Apply percentage discounts to order totals",
            self.edit(
                "shop/orders.py",
                'def order_total(order) -> Decimal:\n    return sum((line.price * line.quantity for line in order.lines), Decimal("0"))\n',
                'def order_total(order, discount_pct: int = 0) -> Decimal:\n    gross = sum((line.price * line.quantity for line in order.lines), Decimal("0"))\n    return gross * (100 - discount_pct) / 100\n',
            ),
        )
        feature_head = self.commit(
            "Show discounted subtotal in the web client",
            self.edit(
                "web/src/pricing.ts",
                "export function subtotal(lines: Line[]): number {",
                "export function discounted(lines: Line[], pct: number): number {\n  return subtotal(lines) * (1 - pct / 100);\n}\n\nexport function subtotal(lines: Line[]): number {",
            ),
        )
        self.push("2. feature branch: 2 new commits", "feature/discounts", base, feature_head)

        fixup = self.commit(
            "Validate discount range",
            self.edit(
                "shop/orders.py",
                "    gross = sum(",
                '    if not 0 <= discount_pct <= 100:\n        raise ValueError("discount_pct must be between 0 and 100")\n    gross = sum(',
            ),
        )
        self.push(
            "3. feature branch: review follow-up commit", "feature/discounts", feature_head, fixup
        )

        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--ff-only", "feature/discounts")
        self.push("4. fast-forward merge into main (same 3 commits)", "main", base, fixup)

        hotfix = self.commit(
            "Hotfix: log payment failures",
            self.edit(
                "shop/payments.py",
                "    response = client.charge(amount=order.total, token=card_token)\n",
                '    try:\n        response = client.charge(amount=order.total, token=card_token)\n    except client.Error:\n        log.exception("charge failed for order %s", order.id)\n        raise\n',
            ),
        )
        self.push("5. hotfix on main", "main", fixup, hotfix)

        self.git("checkout", "-q", "-b", "release/1.0", base)
        picked = (self.git("cherry-pick", "-x", hotfix), self.git("rev-parse", "HEAD"))[1]
        self.push(
            "6. cherry-pick hotfix onto release/1.0 (new SHA)", "release/1.0", ZERO_SHA, picked
        )

        self.git("checkout", "-q", "main")
        self.git("revert", "--no-edit", hotfix)
        reverted = self.git("rev-parse", "HEAD")
        self.push("7. revert hotfix on main", "main", hotfix, reverted)
        self.git("revert", "--no-edit", reverted)
        reapplied = self.git("rev-parse", "HEAD")
        self.push("8. re-apply hotfix (revert of the revert)", "main", reverted, reapplied)

        self.git("checkout", "-q", "-b", "feature/profile")
        profile = self.commit(
            "Prefer full name for display",
            self.edit(
                "shop/users.py",
                "    return user.nickname or",
                "    return user.full_name or user.nickname or",
            ),
        )
        self.push("9. new feature branch commit", "feature/profile", reapplied, profile)
        self.git(
            "commit", "-q", "--amend", "-m", "Prefer the user's full name when displaying users"
        )
        amended = self.git("rev-parse", "HEAD")
        self.push(
            "10. amend message + force-push (same tree)",
            "feature/profile",
            profile,
            amended,
            forced=True,
        )

        self.git("checkout", "-q", "main")
        docs = self.commit(
            "Document discounts",
            self.edit("README.md", "benchmark.\n", "benchmark.\n\nSupports order discounts.\n"),
        )
        self.push("11. unrelated docs change on main", "main", reapplied, docs)

        self.git("checkout", "-q", "feature/profile")
        self.git("rebase", "-q", "main")
        rebased = self.git("rev-parse", "HEAD")
        self.push(
            "12. rebase feature onto main + force-push",
            "feature/profile",
            amended,
            rebased,
            forced=True,
        )

        self.git("checkout", "-q", "main")
        self.git("merge", "-q", "--ff-only", "feature/profile")
        self.push("13. fast-forward merge feature/profile", "main", docs, rebased)

        fresh = self.commit(
            "Round totals to cents",
            self.edit(
                "shop/orders.py",
                "    return gross * (100 - discount_pct) / 100\n",
                '    return (gross * (100 - discount_pct) / 100).quantize(Decimal("0.01"))\n',
            ),
        )
        self.push("14. new change on main", "main", rebased, fresh)

    def report(self) -> None:
        calls = sum(r[1] for r in self.rows)
        hits = sum(r[2] for r in self.rows)
        total = calls + hits
        without_cache = total
        print("\n" + "-" * 86)
        print(f"  Reviews served: {total}   LLM calls: {calls}   cache hits: {hits}")
        if total:
            print(
                f"  LLM calls avoided: {hits / without_cache:.1%}  "
                f"(without the cache every review would have been a model call: {without_cache})"
            )
        stats = self.stats()
        print(
            f"  Tokens saved: {stats.get('tokens_saved', 0):,}   generation time saved: {stats.get('generation_ms_saved', 0) / 1000:.1f}s"
        )
        print(
            "  Note: the rate reflects this workflow mix; teams that merge via fast-forward, cherry-pick"
        )
        print(
            "  release fixes, or rebase often will see more hits; squash-merge-only teams will see fewer."
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--api", default=os.environ.get("CODELENS_URL", "http://localhost:8000"))
    parser.add_argument(
        "--secret", default=os.environ.get("GITHUB_WEBHOOK_SECRET", "dev-secret-change-me")
    )
    parser.add_argument("--repo", default=f"bench/storefront-{datetime.now(UTC):%Y%m%d-%H%M%S}")
    parser.add_argument("--repos-dir", type=Path, default=ROOT / ".demo-repos")
    args = parser.parse_args()
    bench = Bench(args.api, args.secret, args.repo, args.repos_dir)
    bench.run()
    bench.report()


if __name__ == "__main__":
    main()
