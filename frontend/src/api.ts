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

export interface Commit {
  id: number;
  sha: string;
  ref: string;
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

// Optional API token: open the UI once with ?token=... and it is remembered for this browser.
const TOKEN_KEY = "codelens_token";
function readToken(): string | null {
  try {
    const fromUrl = new URLSearchParams(window.location.search).get("token");
    if (fromUrl) localStorage.setItem(TOKEN_KEY, fromUrl);
    return fromUrl ?? localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}
const token = readToken();

async function get<T>(path: string, params?: Record<string, string | number | boolean | undefined>): Promise<T> {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value !== undefined) query.set(key, String(value));
  }
  const response = await fetch(`/api${path}${query.size ? `?${query}` : ""}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json() as Promise<T>;
}

export const api = {
  repos: () => get<Repo[]>("/repos"),
  commits: (repo: string, beforeId?: number) =>
    get<Page<Commit>>(`/repos/${repo}/commits`, { limit: 15, before_id: beforeId }),
  review: (id: number) => get<ReviewDetail>(`/reviews/${id}`),
  stats: (repo?: string) => get<Stats>("/stats", { repo }),
  streamUrl: (id: number) => `/api/reviews/${id}/stream${token ? `?token=${encodeURIComponent(token)}` : ""}`,
};
