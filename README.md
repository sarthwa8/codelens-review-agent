# CodeLens

Real-time, repo-aware LLM code review for GitHub pushes.

A push webhook is acknowledged in milliseconds. A Celery worker then parses each changed file with
**tree-sitter**, retrieves similar code from the **same repository** out of **ChromaDB**, asks an
LLM (Anthropic, OpenAI, Ollama, or an offline fake) for a review, and streams it **token by token**
to a React dashboard over **Server-Sent Events**. Every review is persisted in **PostgreSQL**. A
**content-hash cache** skips the LLM entirely when identical code shows up again, as it does after
fast-forward merges, cherry-picks, rebases, and revert/re-apply.

```
GitHub ──push──▶ FastAPI /webhooks/github ── verify HMAC · dedupe delivery · enqueue · 202
                        │
                        ▼ Redis (Celery broker)
                 Celery worker
                   ├─ fetch commit files + full file contents (GitHub API or local git)
                   ├─ tree-sitter: changed lines → enclosing functions / classes
                   ├─ ChromaDB: similar code elsewhere in the repo (default-branch index)
                   ├─ cache key → Postgres claim ──hit──▶ reuse result (no LLM call)
                   ├─ LLM adapter (anthropic | openai | ollama | fake), streamed
                   └─ XADD tokens → Redis Stream per result
Browser ──SSE /api/reviews/{id}/stream──▶ FastAPI ── XREAD (replay + tail) ──▶ Redis Stream
        ──REST /api/repos/{owner}/{name}/reviews, /api/stats ─────────────────▶ PostgreSQL
```

## Quick start

```bash
cp .env.example .env
docker compose up --build
```

| Service | URL |
|---|---|
| Dashboard | http://localhost:3000 |
| API + OpenAPI docs | http://localhost:8000/docs |
| Webhook endpoint | `POST http://localhost:8000/webhooks/github` |

Postgres, Redis, ChromaDB, migrations, the API, the Celery worker and the frontend all start from
that one command, with health-checked startup ordering.

### Try it without GitHub

The stack can read commits from local git repositories in `./.demo-repos/<owner>/<name>`:

```bash
SOURCE_MODE=local docker compose up -d --build
python scripts/replay_benchmark.py          # builds a demo repo, replays 14 pushes, prints cache stats
```

Or send a signed webhook for your own local repository:

```bash
python scripts/send_webhook.py <owner>/<name> --commits 3
```

### Connect a real repository

1. Set `GITHUB_WEBHOOK_SECRET`, `GITHUB_TOKEN` (needed for private repos and rate limits) and an LLM
   provider in `.env`, for example `LLM_PROVIDER=anthropic` with `ANTHROPIC_API_KEY`.
2. Expose port 8000 (for example with `gh webhook forward`, `ngrok`, or a real deployment).
3. In the repository, add a webhook: payload URL `…/webhooks/github`, content type `application/json`,
   the same secret, and the **push** event.

## Design decisions and flaws fixed from the original spec

| Problem in the spec | Consequence | What CodeLens does |
|---|---|---|
| `content_hash` UNIQUE on `reviews`, but every review must be stored | A duplicate commit can't be recorded, so you lose either the cache or the history | Split into `review_results` (the cache, unique key) and `reviews` (the audit log, one row per commit and file, with a `cache_hit` flag) |
| Cache key = hash(content + diff) | A model or prompt change would serve stale reviews; plain concatenation is ambiguous | SHA-256 over length-prefixed fields: prompt version, provider/model, repo, language, content, patch |
| Cache shared across repositories | Reviews quote retrieved code, which could leak one repo's code into another's reviews | Keys are scoped per repository |
| Concurrent duplicates (push to a branch, then merge) | Both workers miss the cache and call the LLM | `INSERT … ON CONFLICT DO NOTHING` claim: one owner generates, the rest follow its stream. Leases expire if the owner crashes |
| Failures | Partial or errored output could be cached forever | A result is `complete` only on success. A failed result is taken over by the next attempt |
| Webhook fetches diffs from GitHub before responding | Seconds of latency and rate-limit stalls | The webhook only verifies, dedupes and enqueues. All GitHub calls happen in the worker |
| Celery `.delay()` inside an async handler | Blocking I/O stalls the event loop under load | Enqueue runs in the threadpool |
| Redis pub/sub for streaming | Late joiners, reconnects and cache hits (done in milliseconds) see nothing | **Redis Streams**: replayable. SSE event ids are stream ids, so `Last-Event-ID` resumes. Finished reviews are served from Postgres |
| Indexing "on first webhook" inside the review flow | The first review blocks for minutes, and parallel pushes index twice | Separate `index` queue with an atomic `index_status` claim. Reviews run without RAG until the index is ready |
| Index updated on every push | Unmerged branches pollute retrieval, and deleted code stays searchable | Only default-branch pushes update the index. Files are replaced (not appended) and removed files are deleted |
| Similarity search for a changed function | The top hit is its own previous version | Same-file results are excluded and a distance cutoff applies |
| Status enum pending/streaming/complete | Nowhere to record binary, generated, or oversized files, or failures | Added `skipped` (with a reason) and `failed` (with the error) |
| `GET /repos/{repo}/reviews` | Repo names contain `/` | `GET /api/repos/{owner}/{name}/reviews` with filters and cursor pagination |
| "~40% fewer LLM calls" as a target | Depends on workflow, not on code | Measured: `/api/stats` plus `scripts/replay_benchmark.py` (see results below) |
| Code under review is untrusted | Prompt injection, and XSS through model markdown | Content-hash-derived fences, a data-only system prompt, no model tools, markdown rendered without raw HTML, secret redaction before sending |
| No migrations or health checks | Schema drift and startup races | Alembic (`alembic check` in CI) plus health-gated `depends_on` |
| SSE behind nginx | Buffered output arrives all at once at the end | `proxy_buffering off`, `X-Accel-Buffering: no`, 15s heartbeats |

## Repository layout

```
app/
  main.py                 app factory, health checks, router wiring
  config.py               settings (env / .env)
  webhooks/github.py      HMAC verification, dedupe, enqueue
  celery_app.py           broker config: acks_late, separate index queue
  tasks/                  process_push → review_commit → review_file; index_repo, update_index
  review/pipeline.py      per-file orchestration (cache claim → parse → RAG → LLM → stream)
  review/prompt.py        prompt construction, budgets, injection fencing
  review/redact.py        secret redaction
  cache/keys.py           cache-key definition (design notes inline)
  cache/service.py        claim / follow / complete / fail with row-level locking
  parsing/                unified-diff parsing, tree-sitter unit extraction, language rules
  rag/                    embeddings, Chroma store, retriever
  llm/                    provider adapter interface plus anthropic/openai/ollama/fake
  sources/                GitHub REST API and local-git source providers
  streaming/              Redis Streams publisher, SSE endpoint
  api/                    REST history API, stats, token auth
  db/                     SQLAlchemy models and session
alembic/                  migrations
frontend/                 React + Vite dashboard (nginx image proxies /api with SSE settings)
scripts/                  send_webhook.py, loadtest.py, replay_benchmark.py
tests/                    webhook, cache, parsing, RAG/prompt, streaming, end-to-end
```

## API

| Method | Path | Description |
|---|---|---|
| `POST` | `/webhooks/github` | GitHub push webhook (HMAC-verified) |
| `GET` | `/api/repos` | Onboarded repositories with index status and activity |
| `GET` | `/api/repos/{owner}/{name}/commits` | Commits (per ref) with their file reviews, cursor-paginated |
| `GET` | `/api/repos/{owner}/{name}/reviews` | Full review history. Filters: `commit_sha`, `status`, `cache_hit`, `file_path`, `before_id` |
| `GET` | `/api/reviews/{id}` | Review text, diff, and audit metadata (model, tokens, latency, RAG sources) |
| `GET` | `/api/reviews/{id}/stream` | SSE: `reset`, `status`, `delta`, `snapshot`, `retrying`, `skipped`, `done`, `failed` |
| `GET` | `/api/stats?repo=owner/name` | LLM calls, cache hits, hit rate, tokens and time saved |
| `GET` | `/healthz`, `/readyz` | Liveness, and readiness (Postgres + Redis) |

Set `API_TOKEN` to require `Authorization: Bearer <token>` (SSE also accepts `?token=`).

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
docker compose up -d postgres redis
.venv/bin/pytest                      # uses a separate codelens_test database
.venv/bin/ruff check . && .venv/bin/mypy
cd frontend && npm install && npm run dev   # http://localhost:5173, proxies /api to :8000
```

CI (`.github/workflows/ci.yml`) runs ruff, mypy, `alembic check`, pytest against a Postgres
service, the frontend typecheck and build, and both Docker image builds on every push.

## Results

Measured on a laptop running the full `docker compose` stack (8-CPU Docker VM, fake LLM provider).

### Webhook latency: `scripts/loadtest.py -n 3000 -c 50`

| | p50 | p95 | p99 | max | throughput |
|---|---|---|---|---|---|
| Inside the Docker network | 31 ms | 73 ms | 211 ms | 306 ms | ~1,270 req/s |
| From the host via port forwarding | 43 ms | 96 ms | 209 ms | 328 ms | ~950 req/s |

All requests returned `202` and p99 stays well under the 1 s requirement while the worker processes
the resulting pushes at the same time. Note: a single Python client process driving 50 connections
saturates itself and reports p99 over 1 s even though the API sits below one core, so the load
tester spreads the same total concurrency across processes.

### Cache effectiveness: `scripts/replay_benchmark.py`

| Step | LLM calls | Cache hits |
|---|---|---|
| 1. initial import (7 files) | 7 | 0 |
| 2. feature branch: 2 new commits | 2 | 0 |
| 3. feature branch: review follow-up | 1 | 0 |
| 4. fast-forward merge into main | 0 | 3 |
| 5. hotfix on main | 1 | 0 |
| 6. cherry-pick hotfix onto release/1.0 | 0 | 1 |
| 7. revert hotfix | 1 | 0 |
| 8. re-apply hotfix | 0 | 1 |
| 9. new feature branch commit | 1 | 0 |
| 10. amend message + force-push | 0 | 1 |
| 11. unrelated docs change on main | 1 | 0 |
| 12. rebase feature onto main + force-push | 0 | 2 |
| 13. fast-forward merge feature | 0 | 1 |
| 14. new change on main | 1 | 0 |
| **Total** | **15** | **9** |

**37.5% of LLM calls avoided** (53% excluding the one-off initial import). Every
duplicate-content workflow was a hit and every genuinely new change was a miss. The rate depends on
how a team works: squash-merge-only workflows produce fewer duplicates.

### Bugs found by running the stack (now covered by tests)

- **Repo upsert race:** `ON CONFLICT (github_id)` doesn't arbitrate the UNIQUE `full_name` index,
  so concurrent first pushes for a new repository could raise `UniqueViolation`. The upsert now
  retries and tombstones a stale name when a different repository takes it over.
- **Index retry storm:** a repository whose indexing failed was re-indexed on every push (945
  attempts during one load test). Failed indexes now back off for 15 minutes.
- **Follower status race:** a follower attaching to a result at the moment the owner completed
  could stay `streaming` forever. It now reads the result `FOR SHARE`.
- **Cold-start first token:** the first review in each worker process loaded the ONNX embedding
  model inline (about 6 s before the first token). Workers now warm it up at process start.
