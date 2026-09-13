import { useEffect, useState } from "react";
import type { ReviewStatus, Stats } from "./api";

export function StatusBadge({ status }: { status: ReviewStatus | string }) {
  return <span className={`badge badge-${status}`}>{status}</span>;
}

export function CacheBadge({ hit }: { hit: boolean }) {
  return hit ? (
    <span className="badge badge-cache" title="Served from the content-hash cache — no LLM call">
      cache hit
    </span>
  ) : null;
}

export function Sha({ sha }: { sha: string }) {
  return <code className="sha">{sha.slice(0, 7)}</code>;
}

export function branchName(ref: string) {
  return ref.replace(/^refs\/heads\//, "");
}

export function timeAgo(iso: string | null) {
  if (!iso) return "—";
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return `${Math.floor(seconds)}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return new Date(iso).toLocaleDateString();
}

export function StatsBar({ stats }: { stats: Stats | null }) {
  if (!stats) return <div className="stats stats-loading" />;
  const served = stats.llm_calls + stats.cache_hits;
  return (
    <div className="stats">
      <Metric label="Reviews" value={stats.reviews_total.toLocaleString()} />
      <Metric label="LLM calls" value={stats.llm_calls.toLocaleString()} />
      <Metric label="Cache hits" value={stats.cache_hits.toLocaleString()} />
      <Metric
        label="LLM calls avoided"
        value={served ? `${stats.llm_calls_avoided_pct.toFixed(1)}%` : "—"}
        emphasis
        bar={served ? stats.cache_hit_rate : undefined}
      />
      <Metric label="Tokens saved" value={stats.tokens_saved.toLocaleString()} />
      <Metric label="Generation time saved" value={formatDuration(stats.generation_ms_saved)} />
    </div>
  );
}

function Metric({ label, value, emphasis, bar }: { label: string; value: string; emphasis?: boolean; bar?: number }) {
  return (
    <div className={`metric${emphasis ? " metric-emphasis" : ""}`}>
      <div className="metric-value">{value}</div>
      <div className="metric-label">{label}</div>
      {bar !== undefined && (
        <div className="meter" aria-hidden>
          <div className="meter-fill" style={{ width: `${Math.round(bar * 100)}%` }} />
        </div>
      )}
    </div>
  );
}

export function formatDuration(ms: number | null) {
  if (ms === null) return "—";
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  return `${(ms / 60_000).toFixed(1)} min`;
}

/** Re-run `load` on an interval while `active` is true (used to watch in-progress reviews). */
export function usePolling<T>(load: () => Promise<T>, deps: unknown[], active: (data: T | null) => boolean, ms = 2500) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      try {
        const next = await load();
        if (cancelled) return;
        setData(next);
        setError(null);
        if (active(next)) timer = setTimeout(tick, ms);
      } catch (e) {
        if (cancelled) return;
        setError((e as Error).message);
        timer = setTimeout(tick, ms * 2);
      }
    };
    tick();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { data, error };
}

export function DiffView({ patch }: { patch: string }) {
  return (
    <pre className="diff">
      {patch.split("\n").map((line, i) => {
        const kind = line.startsWith("@@") ? "hunk" : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "ctx";
        return (
          <span key={i} className={`diff-line diff-${kind}`}>
            {line || " "}
            {"\n"}
          </span>
        );
      })}
    </pre>
  );
}
