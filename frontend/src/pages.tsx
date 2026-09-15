import { useCallback, useEffect, useState } from "react";
import Markdown from "react-markdown";
import { Link, useParams } from "react-router";
import remarkGfm from "remark-gfm";
import { api, type Commit, type PublishStatus, type ReviewDetail } from "./api";
import {
  CacheBadge,
  DiffView,
  Sha,
  StatsBar,
  StatusBadge,
  branchName,
  formatDuration,
  timeAgo,
  usePolling,
} from "./components";
import { useMe } from "./me";
import { useReviewStream, type StreamPhase } from "./useReviewStream";

export function RepoListPage() {
  const repos = usePolling(api.repos, [], () => true, 5000);
  const stats = usePolling(() => api.stats(), [], () => true, 5000);

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Repositories</h1>
          <p className="muted">Every push is parsed, matched against similar code in the same repo, and reviewed live.</p>
        </div>
      </header>
      <StatsBar stats={stats.data} />
      {repos.error && <p className="error">Could not load repositories: {repos.error}</p>}
      {repos.data?.length === 0 && (
        <div className="empty">
          <h2>No reviews yet</h2>
          <p>
            Point a GitHub webhook at <code>/webhooks/github</code> (push events), or run{" "}
            <code>python scripts/send_webhook.py</code> against a local repository.
          </p>
        </div>
      )}
      <ul className="repo-grid">
        {repos.data?.map((repo) => (
          <li key={repo.id}>
            <Link to={`/repos/${repo.full_name}`} className="repo-card">
              <div className="repo-name">{repo.full_name}</div>
              <div className="repo-meta">
                <span>{repo.review_count} reviews</span>
                <span>·</span>
                <span>active {timeAgo(repo.last_activity_at)}</span>
              </div>
              <div className={`index index-${repo.index_status}`} title={repo.index_error ?? undefined}>
                RAG index: {repo.index_status}
                {repo.index_status === "ready" && ` · ${repo.indexed_chunks} chunks`}
              </div>
            </Link>
          </li>
        ))}
      </ul>
    </>
  );
}

const PUBLISH_LABEL: Partial<Record<PublishStatus, string>> = {
  publishing: "posting to GitHub",
  published: "posted to GitHub",
  failed: "GitHub post failed",
};

function PublishBadge({ status, skipped }: { status: PublishStatus; skipped: boolean }) {
  const label = PUBLISH_LABEL[status];
  if (!label || skipped) return null;
  return <span className={`badge badge-publish-${status}`}>{label}</span>;
}

function unitUrl(githubUrl: string, fullName: string, commit: Commit): string {
  return commit.kind === "pull_request"
    ? `${githubUrl}/${fullName}/pull/${commit.pr_number}`
    : `${githubUrl}/${fullName}/commit/${commit.sha}`;
}

export function RepoPage() {
  const { owner = "", name = "" } = useParams();
  const fullName = `${owner}/${name}`;
  const githubUrl = useMe()?.github_web_url;
  const [olderPages, setOlderPages] = useState<Commit[]>([]);
  const [cursor, setCursor] = useState<number | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);

  // Keep polling the newest page: new pushes arrive and in-progress reviews change status.
  const latest = usePolling(() => api.commits(fullName), [fullName], () => true, 3000);
  const stats = usePolling(() => api.stats(fullName), [fullName], () => true, 3000);

  useEffect(() => {
    setOlderPages([]);
    setCursor(null);
  }, [fullName]);
  useEffect(() => {
    if (latest.data && !olderPages.length) setCursor(latest.data.next_before_id);
  }, [latest.data, olderPages.length]);

  const loadMore = useCallback(async () => {
    if (!cursor) return;
    setLoadingMore(true);
    try {
      const page = await api.commits(fullName, cursor);
      setOlderPages((prev) => [...prev, ...page.items]);
      setCursor(page.next_before_id);
    } finally {
      setLoadingMore(false);
    }
  }, [cursor, fullName]);

  const seen = new Set<number>();
  const commits = [...(latest.data?.items ?? []), ...olderPages].filter((c) => !seen.has(c.id) && seen.add(c.id));

  return (
    <>
      <nav className="crumbs">
        <Link to="/">Repositories</Link> / <span>{fullName}</span>
      </nav>
      <header className="page-head">
        <h1>{fullName}</h1>
      </header>
      <StatsBar stats={stats.data} />
      {latest.error && <p className="error">Could not load commits: {latest.error}</p>}
      <ol className="commits">
        {commits.map((commit) => (
          <li key={commit.id} className="commit">
            <div className="commit-head">
              <Sha sha={commit.sha} />
              {commit.kind === "pull_request" ? (
                <span className="badge badge-pr">PR #{commit.pr_number}</span>
              ) : (
                <span className="branch">{branchName(commit.ref)}</span>
              )}
              <span className="commit-message">{commit.message.split("\n")[0]}</span>
              <PublishBadge status={commit.publish_status} skipped={!!commit.skip_reason} />
              {githubUrl && (
                <a className="muted small" href={unitUrl(githubUrl, fullName, commit)} target="_blank" rel="noreferrer">
                  GitHub ↗
                </a>
              )}
              <span className="muted commit-when">
                {commit.author ?? "unknown"} · {timeAgo(commit.created_at)}
              </span>
            </div>
            {commit.skip_reason && <p className="muted small unit-note">Not reviewed separately: {commit.skip_reason}.</p>}
            <ul className="files">
              {commit.reviews.map((review) => (
                <li key={review.id}>
                  <Link to={`/reviews/${review.id}`} className="file-row">
                    <code className="file-path">{review.file_path}</code>
                    <span className="file-badges">
                      <CacheBadge hit={review.cache_hit} />
                      <StatusBadge status={review.status} />
                    </span>
                    {review.skip_reason && <span className="muted skip">{review.skip_reason}</span>}
                  </Link>
                </li>
              ))}
              {commit.reviews.length === 0 && !commit.skip_reason && (
                <li className="muted file-row">Waiting for the worker…</li>
              )}
            </ul>
          </li>
        ))}
      </ol>
      {cursor && (
        <button className="more" onClick={loadMore} disabled={loadingMore}>
          {loadingMore ? "Loading…" : "Older commits"}
        </button>
      )}
    </>
  );
}

const PHASE_LABEL: Record<StreamPhase, string> = {
  connecting: "Connecting…",
  queued: "Queued — waiting for a worker",
  streaming: "Generating",
  following: "Identical code is already being reviewed — sharing that stream",
  retrying: "Model error — retrying",
  reconnecting: "Connection lost — reconnecting",
  complete: "Complete",
  skipped: "Skipped",
  failed: "Failed",
};

export function ReviewPage() {
  const id = Number(useParams().id);
  const stream = useReviewStream(id);
  const [detail, setDetail] = useState<ReviewDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const finished = ["complete", "skipped", "failed"].includes(stream.phase);

  useEffect(() => {
    // Load metadata up front, and again once the stream finishes (tokens, latency, RAG sources).
    api.review(id).then(setDetail, (e: Error) => setError(e.message));
  }, [id, finished]);

  if (error) return <p className="error">Could not load review: {error}</p>;
  const live = !finished && stream.phase !== "connecting";

  return (
    <>
      <nav className="crumbs">
        <Link to="/">Repositories</Link> /{" "}
        {detail ? <Link to={`/repos/${detail.repo_full_name}`}>{detail.repo_full_name}</Link> : "…"} /{" "}
        <span>review #{id}</span>
      </nav>
      <header className="page-head review-head">
        <div>
          <h1 className="file-title">{detail?.file_path ?? "…"}</h1>
          {detail && (
            <p className="muted">
              <Sha sha={detail.commit_sha} /> in{" "}
              {detail.kind === "pull_request" ? (
                <span className="badge badge-pr">PR #{detail.pr_number}</span>
              ) : (
                <span className="branch">{branchName(detail.ref)}</span>
              )}{" "}
              —{" "}
              {detail.commit_message.split("\n")[0]}
            </p>
          )}
        </div>
        <div className="file-badges">
          <CacheBadge hit={stream.cacheHit || !!detail?.cache_hit} />
          <span className={`phase phase-${stream.phase}`}>
            {live && <span className="pulse" aria-hidden />}
            {PHASE_LABEL[stream.phase]}
          </span>
        </div>
      </header>

      <div className="review-layout">
        <section className="panel review-panel" aria-live="polite" aria-busy={live}>
          <div className="panel-title">
            <span>Review</span>
            {stream.deltas > 0 && (
              <span className="muted">
                {stream.deltas} streamed chunks · first token {stream.firstTokenMs} ms
              </span>
            )}
          </div>
          {stream.message && <p className={stream.phase === "failed" ? "error" : "notice"}>{stream.message}</p>}
          <div className={`markdown${live ? " is-streaming" : ""}`}>
            {/* react-markdown never renders raw HTML: model output can't inject markup/scripts. */}
            <Markdown remarkPlugins={[remarkGfm]}>{stream.text}</Markdown>
            {live && <span className="caret" aria-hidden />}
          </div>
          {!stream.text && finished && stream.phase !== "failed" && (
            <p className="muted">{stream.phase === "skipped" ? "This file was not reviewed." : "Empty review."}</p>
          )}
        </section>

        <aside className="side">
          {detail?.result && (
            <section className="panel">
              <div className="panel-title">Audit</div>
              <dl className="facts">
                <dt>Model</dt>
                <dd>
                  {detail.result.provider} / {detail.result.model}
                </dd>
                <dt>Prompt</dt>
                <dd>{detail.result.prompt_version}</dd>
                <dt>Tokens</dt>
                <dd>
                  {detail.result.input_tokens ?? "—"} in · {detail.result.output_tokens ?? "—"} out
                </dd>
                <dt>Generation</dt>
                <dd>{formatDuration(detail.result.latency_ms)}</dd>
                <dt>Attempts</dt>
                <dd>{detail.result.attempts}</dd>
                <dt>Content hash</dt>
                <dd>
                  <code title={detail.content_hash ?? ""}>{detail.content_hash?.slice(0, 16)}…</code>
                </dd>
                {!!detail.result.context?.redactions && (
                  <>
                    <dt>Redacted</dt>
                    <dd>{detail.result.context.redactions} secret(s) before sending</dd>
                  </>
                )}
              </dl>
              {!!detail.result.context?.changed_units?.length && (
                <>
                  <div className="panel-subtitle">Changed units (tree-sitter)</div>
                  <ul className="list">
                    {detail.result.context.changed_units.map((unit) => (
                      <li key={unit}>
                        <code>{unit}</code>
                      </li>
                    ))}
                  </ul>
                </>
              )}
              <div className="panel-subtitle">Similar code used as context (RAG)</div>
              {detail.result.context?.similar_code?.length ? (
                <ul className="list">
                  {detail.result.context.similar_code.map((s) => (
                    <li key={s.location}>
                      <code>{s.location}</code> <span className="muted">distance {s.distance.toFixed(3)}</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="muted small">
                  {finished
                    ? "None — index not ready yet, or nothing similar enough."
                    : "Recorded when the review completes."}
                </p>
              )}
            </section>
          )}
          {detail?.patch && (
            <section className="panel">
              <div className="panel-title">Diff</div>
              <DiffView patch={detail.patch} />
            </section>
          )}
        </aside>
      </div>
    </>
  );
}
