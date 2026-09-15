export type ReviewStatus = "pending" | "streaming" | "complete" | "failed" | "skipped";

export interface Repo {
  id: number;
  full_name: string;
  default_branch: string;
  index_status: "none" | "indexing" | "ready" | "failed";
  indexed_chunks: number;
  indexed_at: string | null;
  embedding_model: string | null;
  index_error: string | null;
  review_count: number;
  last_activity_at: string | null;
}

export interface ReviewSummary {
  id: number;
  file_path: string;
  change_type: string;
  language: string | null;
  status: ReviewStatus;
  cache_hit: boolean;
  skip_reason: string | null;
  created_at: string;
  commit_sha: string;
  ref: string;
}

export type PublishStatus = "pending" | "publishing" | "published" | "failed" | "skipped";

export interface Commit {
  id: number;
  sha: string;
  ref: string;
  kind: "push" | "pull_request";
  pr_number: number | null;
  skip_reason: string | null;
  publish_status: PublishStatus;
  message: string;
  author: string | null;
  committed_at: string | null;
  created_at: string;
  reviews: ReviewSummary[];
}

export interface ReviewResult {
  id: number;
  status: string;
  provider: string;
  model: string;
  prompt_version: string;
  input_tokens: number | null;
  output_tokens: number | null;
  latency_ms: number | null;
  attempts: number;
  context: {
    changed_units?: string[];
    similar_code?: { location: string; distance: number }[];
    redactions?: number;
    patch_truncated?: boolean;
  } | null;
  completed_at: string | null;
}

export interface ReviewDetail extends ReviewSummary {
  repo_full_name: string;
  kind: "push" | "pull_request";
  pr_number: number | null;
  commit_message: string;
  patch: string | null;
  content_hash: string | null;
  error: string | null;
  review_text: string;
  result: ReviewResult | null;
}

export interface Page<T> {
  items: T[];
  next_before_id: number | null;
}

export interface Stats {
  reviews_total: number;
  by_status: Record<string, number>;
  llm_calls: number;
  cache_hits: number;
  cache_hit_rate: number;
  llm_calls_avoided_pct: number;
  tokens_saved: number;
  generation_ms_saved: number;
}

export interface Me {
  auth_mode: "none" | "github";
  authenticated: boolean;
  login: string | null;
  avatar_url: string | null;
  install_url: string | null;
  github_web_url: string;
}

export function signInUrl(): string {
  const next = window.location.pathname + window.location.search;
  return `/auth/login?next=${encodeURIComponent(next)}`;
}

async function get<T>(path: string, params?: Record<string, string | number | boolean | undefined>): Promise<T> {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value !== undefined) query.set(key, String(value));
  }
  // Same-origin requests carry the HttpOnly session cookie automatically.
  const response = await fetch(`/api${path}${query.size ? `?${query}` : ""}`);
  if (response.status === 401) {
    window.location.assign(signInUrl());
    throw new Error("Signing in…");
  }
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json() as Promise<T>;
}

export const api = {
  repos: () => get<Repo[]>("/repos"),
  commits: (repo: string, beforeId?: number) =>
    get<Page<Commit>>(`/repos/${repo}/commits`, { limit: 15, before_id: beforeId }),
  review: (id: number) => get<ReviewDetail>(`/reviews/${id}`),
  stats: (repo?: string) => get<Stats>("/stats", { repo }),
  me: () => fetch("/auth/me").then((r) => r.json() as Promise<Me>),
  // EventSource sends same-origin cookies, so the stream is authorized like any other request.
  streamUrl: (id: number) => `/api/reviews/${id}/stream`,
};
