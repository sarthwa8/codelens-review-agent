#!/usr/bin/env python3
"""Webhook latency load test.

Fires N signed push webhooks (unique delivery ids) at the running API with C concurrent clients
and reports latency percentiles. The requirement under test: the webhook answers well under one
second regardless of LLM latency, because all real work is deferred to Celery.

    python scripts/loadtest.py -n 2000 -c 50

Pushes target a synthetic repository that doesn't exist on disk, so the worker records the
commits and gives up quickly on fetching them. Pass --purge to delete those rows afterwards
(uses `docker compose exec postgres psql`).
"""

import argparse
import asyncio
import os
import statistics
import subprocess
import sys
import time
import uuid
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from webhook_payload import github_id_for, signed_request

SYNTHETIC_REPO = "codelens-loadtest/synthetic"


def payload(i: int) -> dict:
    commits = [
        {
            "id": uuid.uuid4().hex + uuid.uuid4().hex[:8],
            "message": f"load test commit {i}.{n}\n\n" + "Body text. " * 40,
            "timestamp": "2026-09-14T12:00:00Z",
            "author": {"name": "Load Tester", "username": "loadtester"},
            "added": [f"src/module_{k}.py" for k in range(5)],
            "modified": [f"src/service_{k}.py" for k in range(10)],
            "removed": [],
        }
        for n in range(3)
    ]
    return {
        "ref": "refs/heads/main",
        "before": "a" * 40,
        "after": commits[-1]["id"],
        "repository": {
            "id": github_id_for(SYNTHETIC_REPO),
            "full_name": SYNTHETIC_REPO,
            "default_branch": "main",
        },
        "commits": commits,
        "head_commit": commits[-1],
        "pusher": {"name": "loadtester"},
    }


def pct(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1)))]


async def client_loop(
    url: str, secret: str, total: int, concurrency: int, offset: int, start_at: float
):
    requests = [signed_request(payload(offset + i), secret) for i in range(total)]
    latencies: list[float] = []
    statuses: Counter[int | str] = Counter()
    queue: asyncio.Queue[tuple[bytes, dict[str, str]]] = asyncio.Queue()
    for item in requests:
        queue.put_nowait(item)

    async def worker(client: httpx.AsyncClient) -> None:
        while not queue.empty():
            body, headers = queue.get_nowait()
            started = time.perf_counter()
            try:
                response = await client.post(url, content=body, headers=headers)
                statuses[response.status_code] += 1
            except httpx.HTTPError as exc:
                statuses[type(exc).__name__] += 1
            latencies.append((time.perf_counter() - started) * 1000)

    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(timeout=30, limits=limits) as client:
        ping = {**requests[0][1], "X-GitHub-Event": "ping"}
        await asyncio.gather(
            *(client.post(url, content=requests[0][0], headers=ping) for _ in range(concurrency))
        )
        await asyncio.sleep(max(0.0, start_at - time.time()))  # all processes start together
        await asyncio.gather(*(worker(client) for _ in range(concurrency)))
    return latencies, statuses


def client_process(job: tuple[str, str, int, int, int, float]):
    return asyncio.run(client_loop(*job))


def run(url: str, secret: str, total: int, concurrency: int, processes: int) -> None:
    # A single Python process driving dozens of concurrent connections becomes the bottleneck and
    # its own queueing shows up as "server latency". Spread the same total concurrency over processes.
    processes = max(1, min(processes, concurrency))
    start_at = time.time() + 2
    jobs = []
    for p in range(processes):
        share = total // processes + (1 if p < total % processes else 0)
        conc = concurrency // processes + (1 if p < concurrency % processes else 0)
        jobs.append((url, secret, share, conc, p * total, start_at))
    with ProcessPoolExecutor(processes) as pool:
        results = list(pool.map(client_process, jobs))
    elapsed = time.time() - start_at

    latencies = [ms for lat, _ in results for ms in lat]
    statuses: Counter[int | str] = Counter()
    for _, st in results:
        statuses.update(st)
    body_kb = len(signed_request(payload(0), secret)[0]) / 1024
    print(
        f"\nWebhook load test — {total} requests, {concurrency} concurrent "
        f"({processes} client processes), ~{body_kb:.1f} KB payload each"
    )
    print(f"  status codes : {dict(statuses)}")
    print(f"  throughput   : {total / elapsed:,.0f} req/s over {elapsed:.2f}s")
    print(
        f"  latency (ms) : p50 {pct(latencies, 50):.1f} | p95 {pct(latencies, 95):.1f} | "
        f"p99 {pct(latencies, 99):.1f} | max {max(latencies):.1f} | mean {statistics.mean(latencies):.1f}"
    )
    verdict = "PASS" if pct(latencies, 99) < 1000 and statuses.get(202) == total else "FAIL"
    print(f"  requirement  : p99 < 1000 ms with every request accepted → {verdict}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("-n", "--requests", type=int, default=2000)
    parser.add_argument("-c", "--concurrency", type=int, default=50)
    parser.add_argument("-p", "--processes", type=int, default=min(8, os.cpu_count() or 1))
    parser.add_argument(
        "--url",
        default=os.environ.get("CODELENS_URL", "http://localhost:8000") + "/webhooks/github",
    )
    parser.add_argument(
        "--secret", default=os.environ.get("GITHUB_WEBHOOK_SECRET", "dev-secret-change-me")
    )
    parser.add_argument(
        "--purge", action="store_true", help="delete the synthetic repo's rows afterwards"
    )
    args = parser.parse_args()
    run(args.url, args.secret, args.requests, args.concurrency, args.processes)
    if args.purge:
        # Wait for the worker to drain the queue first, or queued pushes would recreate the rows.
        compose = ["docker", "compose", "exec", "-T"]
        for _ in range(300):
            depth = subprocess.run(
                [*compose, "redis", "redis-cli", "llen", "review"], capture_output=True, text=True
            )
            if depth.stdout.strip() == "0":
                break
            time.sleep(1)
        sql = f"DELETE FROM repos WHERE full_name = '{SYNTHETIC_REPO}';"
        subprocess.run(
            ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "codelens", "-c", sql],
            check=False,
        )


if __name__ == "__main__":
    main()
