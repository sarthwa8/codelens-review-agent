# CodeLens

Real-time, repo-aware LLM code review for GitHub pushes and pull requests.

CodeLens runs as a **GitHub App**. When code is pushed or a pull request is opened or updated, the
webhook is acknowledged in milliseconds. A Celery worker then parses each changed file with
**tree-sitter**, retrieves similar code from the **same repository** out of **ChromaDB**, asks an
LLM (**Groq** free tier, Anthropic, OpenAI, Ollama, or an offline fake) for a review, and streams
it **token by token** to a React dashboard over **Server-Sent Events**.

Results go back to GitHub as a **check run** with line annotations and, for pull requests, a
**PR review** with inline comments. People **sign in with GitHub** and only see repositories their
account can read. Every review is kept in **PostgreSQL**, and a **content-hash cache** skips the
LLM entirely when identical code shows up again (fast-forward merges, cherry-picks, rebases,
revert/re-apply).

```
GitHub ──push / pull_request──▶ FastAPI /webhooks/github ── verify HMAC · dedupe · enqueue · 202
                                       │
                                       ▼ Redis (Celery broker)
                                Celery worker
                                  ├─ branch has an open PR? review the PR instead of the push
                                  ├─ GitHub App installation token → changed files + contents
                                  ├─ tree-sitter: changed lines → enclosing functions / classes
                                  ├─ ChromaDB: similar code elsewhere in the repo
                                  ├─ cache key → Postgres claim ──hit──▶ reuse result (no LLM call)
                                  ├─ LLM (groq | anthropic | openai | ollama | fake), streamed
                                  ├─ tokens → Redis Stream per result
                                  └─ all files done → check run + PR review on GitHub
Browser ──sign in with GitHub──▶ /auth/* (PKCE, HttpOnly session cookie)
        ──SSE /api/reviews/{id}/stream──▶ FastAPI ── XREAD (replay + tail) ──▶ Redis Stream
        ──REST /api/...  (only repos the signed-in user can read) ───────────▶ PostgreSQL
```

## Run it

You need Docker, a free [Groq API key](https://console.groq.com/keys), and a GitHub account.

**1. Configure.**

```bash
cp .env.example .env
```

Put your Groq key in `GROQ_API_KEY`, and a random value in `SESSION_SECRET`:

```bash
openssl rand -hex 32
```

**2. Get a public webhook URL (local development).** GitHub must be able to reach your webhook. Open
https://smee.io, click **Start a new channel**, and keep the channel URL for the next step.

**3. Create the GitHub App.** Go to GitHub → Settings → Developer settings → GitHub Apps →
**New GitHub App**:

| Field | Value |
|---|---|
| Homepage URL | `http://localhost:3000` |
| Callback URL | `http://localhost:3000/auth/callback` |
| Expire user authorization tokens | on (default) |
| Webhook URL | your smee channel URL (later: `https://<your-domain>/webhooks/github`) |
| Webhook secret | any random string, also put it in `GITHUB_WEBHOOK_SECRET` |
| Subscribe to events | **Push**, **Pull request** |

Under **Repository permissions**, set exactly these four and leave every other one on *No access*:

| Permission | Access | What CodeLens calls it for |
|---|---|---|
| Checks | **Read and write** | `POST`/`PATCH /repos/{repo}/check-runs` — the "CodeLens" check and its annotations |
| Contents | **Read-only** | `/commits/{sha}`, `/contents/{path}`, `/tarball/{ref}` — the diff, the changed files, the RAG index |
| Pull requests | **Read and write** | `/pulls`, `/pulls/{n}/files`, `POST /pulls/{n}/reviews` — the PR diff and the review |
| Metadata | Read-only | Mandatory; GitHub selects it automatically once you pick any of the above |

No account or organization permissions are needed. `github_app_authorization` (sent when a user revokes
the App) has no checkbox: GitHub always delivers it.

After creating it, copy the **App ID**, **Client ID** and the app's URL name (slug) into `.env`,
generate a **client secret** (`GITHUB_APP_CLIENT_SECRET`), and **generate a private key**. Save the
downloaded file as `secrets/github-app.pem`, which git ignores.

**4. Install the App** on the repositories you want reviewed (App page → **Install App**).

**5. Start everything.**

```bash
docker compose up -d --build
```

Then forward webhooks from smee to CodeLens (leave this running):

```bash
npx smee-client --url <your-smee-channel-url> --target http://localhost:8000/webhooks/github
```

**6. Use it.** Open http://localhost:3000, sign in with GitHub, and push a commit or open a pull
request in an installed repository. The review streams into the dashboard, and the check run and
PR review appear on GitHub when it finishes.

On Groq's free tier (8K tokens per minute), large pushes are reviewed file by file as the rate
limit allows. CodeLens waits for `retry-after` automatically.

### Try it offline (no GitHub, no API key)

```bash
printf 'SOURCE_MODE=local\nLLM_PROVIDER=fake\nAUTH_MODE=none\nFAKE_LLM_DELAY_MS=40\n' > .env
docker compose up -d --build
python scripts/replay_benchmark.py     # builds a demo repo, replays 14 pushes, prints cache stats
```

### Make it live

Run the same `docker compose` stack on a server with a domain and HTTPS in front of port 3000. Set
`PUBLIC_URL=https://<your-domain>`, change the App's callback URL to
`https://<your-domain>/auth/callback` and its webhook URL to `https://<your-domain>/webhooks/github`,
then drop smee. Don't expose the Postgres, Redis or Chroma ports publicly.

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
  webhooks/github.py      HMAC verification, dedupe, routing of push / pull_request / revocation events
  celery_app.py           broker config: acks_late, separate index queue
  tasks/                  process_push / process_pull_request → review_commit → review_file;
                          publish_unit; index_repo, update_index; revoke_user_sessions
  review/pipeline.py      per-file orchestration (cache claim → parse → RAG → LLM → stream)
  review/prompt.py        prompt construction, budgets, injection fencing
  review/redact.py        secret redaction
  review/findings.py      parse model findings (severity, path:line, message)
  review/report.py        check-run and PR-review reports, inline-comment placement, sanitization
  cache/keys.py           cache-key definition (design notes inline)
  cache/service.py        claim / follow / complete / fail with row-level locking
  parsing/                unified-diff parsing, tree-sitter unit extraction, language rules
  rag/                    embeddings, Chroma store, retriever
  llm/                    provider adapter interface plus groq/anthropic/openai/ollama/fake
  github/                 App JWT + installation tokens, REST client, check-run/PR-review publisher
  auth/                   sign in with GitHub: OAuth + PKCE, sessions, repo-scoped viewer
  sources/                GitHub REST API and local-git source providers
  streaming/              Redis Streams publisher, SSE endpoint
  api/                    REST history API and stats, scoped to the signed-in viewer
  db/                     SQLAlchemy models and session
alembic/                  migrations
frontend/                 React + Vite dashboard (nginx image proxies /api with SSE settings)
scripts/                  send_webhook.py, loadtest.py, replay_benchmark.py
tests/                    webhook, cache, parsing, RAG/prompt, streaming, end-to-end
```

## API

| Method | Path | Description |
|---|---|---|
| `POST` | `/webhooks/github` | GitHub App webhook: `push`, `pull_request`, `github_app_authorization` (HMAC-verified) |
| `GET` | `/auth/login`, `/auth/callback` | Sign in with GitHub (OAuth web flow + PKCE) |
| `POST` | `/auth/logout` | End the session |
| `GET` | `/auth/me` | Current user, auth mode, App install link |
| `GET` | `/api/repos` | Onboarded repositories with index status and activity |
| `GET` | `/api/repos/{owner}/{name}/commits` | Review units (pushed commits and PRs) with their file reviews and GitHub publish status, cursor-paginated |
| `GET` | `/api/repos/{owner}/{name}/reviews` | Full review history. Filters: `commit_sha`, `status`, `cache_hit`, `file_path`, `before_id` |
| `GET` | `/api/reviews/{id}` | Review text, diff, and audit metadata (model, tokens, latency, RAG sources) |
| `GET` | `/api/reviews/{id}/stream` | SSE: `reset`, `status`, `delta`, `snapshot`, `retrying`, `skipped`, `done`, `failed` |
| `GET` | `/api/stats?repo=owner/name` | LLM calls, cache hits, hit rate, tokens and time saved |
| `GET` | `/healthz`, `/readyz` | Liveness, and readiness (Postgres + Redis) |

With `AUTH_MODE=github`, every `/api` route (including the SSE stream) requires a session and only
returns repositories the user can read on GitHub. Other repositories answer 404.

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
- **tree-sitter 0.26.0 memory corruption:** walking a large tree segfaulted workers during garbage
  collection, and Python locals were overwritten with `Node` objects. It reproduces 3/3 on 0.26.0
  and never on 0.25.2, so the dependency is pinned below 0.26 with a regression test.
- **False failures published to GitHub:** a generation about to be retried marked its reviews
  `failed`, which could publish a failing check run before the retry ran. Reviews now stay
  `pending` while a retry is queued.
- **Rate limits reported as final failures:** the retry decision was made before the error was
  known, so a Groq 429 after three earlier retries told the browser "failed" despite 30 rate-limit
  retries remaining. One shared `will_retry` rule now drives both the task and the stream.
