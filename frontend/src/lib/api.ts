/**
 * The BFF's only door to FastAPI. Server-side only: the internal bearer token
 * lives in this process and never reaches the browser, which is why neither
 * variable is NEXT_PUBLIC_. A missing variable throws — a dashboard that
 * silently renders nothing looks the same as a healthy empty account.
 *
 * Every read also carries `X-Authorized-Repo-Ids`, the set resolved in
 * `lib/github.ts` from GitHub's installations API. It is a **required argument**
 * rather than something looked up in here, because the caller is the only one that
 * knows whether anybody is signed in — and the API answers 400 rather than
 * unfiltered if it is missing, so forgetting is loud instead of dangerous.
 */

export type RepoSummary = {
  id: number;
  full_name: string;
  private: boolean;
  active: boolean;
  job_count: number;
  last_job_at: string | null;
};

export type JobRow = {
  id: number;
  name: string;
  run_id: number;
  run_attempt: number;
  head_sha: string;
  status: string | null;
  conclusion: string | null;
  started_at: string | null;
  completed_at: string | null;
  duration_seconds: number | null;
};

/** One row of the public board. It names its repo, because the board spans repos. */
export type PublicFlakyRow = {
  repo_id: number;
  repo_full_name: string;
  workflow_id: number | null;
  job_name: string;
  opportunities: number;
  failures: number;
  flakes: number;
  last_flake_at: string | null;
  flake_rate: number | null;
  wilson_lower: number | null;
  wilson_upper: number | null;
};

function required(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(
      `${name} is not set. The BFF cannot reach the API without it.`,
    );
  }
  return value;
}

async function apiGet<T>(path: string, authorizedRepoIds: number[]): Promise<T> {
  const base = required("API_BASE_URL").replace(/\/$/, "");
  const token = required("INTERNAL_API_TOKEN");

  const response = await fetch(`${base}${path}`, {
    headers: {
      Authorization: `Bearer ${token}`,
      "X-Authorized-Repo-Ids": authorizedRepoIds.join(","),
    },
    cache: "no-store",
  });

  if (!response.ok) {
    throw new Error(`GET ${path} returned ${response.status}`);
  }
  return (await response.json()) as T;
}

export function listRepos(authorizedRepoIds: number[]): Promise<RepoSummary[]> {
  return apiGet<RepoSummary[]>("/api/repos", authorizedRepoIds);
}

export function listJobs(
  repoId: number,
  authorizedRepoIds: number[],
  limit = 50,
): Promise<JobRow[]> {
  return apiGet<JobRow[]>(`/api/repos/${repoId}/jobs?limit=${limit}`, authorizedRepoIds);
}

/**
 * The public board, and the one read that carries **no credentials at all** — not the
 * bearer token and not an authorized-repo header. That is deliberate rather than
 * economical: the endpoint takes no repo id and filters `private = false` in its own
 * SQL, so there is nothing for a header to widen, and sending one anyway would suggest
 * the boundary lives in the caller. It still goes through the BFF, because the browser
 * never calls FastAPI directly.
 */
export async function listPublicFlaky(
  windowDays = 30,
  limit = 50,
): Promise<PublicFlakyRow[]> {
  const base = required("API_BASE_URL").replace(/\/$/, "");
  const response = await fetch(
    `${base}/public/flaky?window_days=${windowDays}&limit=${limit}`,
    { cache: "no-store" },
  );

  if (!response.ok) {
    throw new Error(`GET /public/flaky returned ${response.status}`);
  }
  return (await response.json()) as PublicFlakyRow[];
}
