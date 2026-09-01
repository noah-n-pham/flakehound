/**
 * The BFF's only door to FastAPI. Server-side only: the internal bearer token
 * lives in this process and never reaches the browser, which is why neither
 * variable is NEXT_PUBLIC_. A missing variable throws — a dashboard that
 * silently renders nothing looks the same as a healthy empty account.
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

function required(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(
      `${name} is not set. The BFF cannot reach the API without it.`,
    );
  }
  return value;
}

async function apiGet<T>(path: string): Promise<T> {
  const base = required("API_BASE_URL").replace(/\/$/, "");
  const token = required("INTERNAL_API_TOKEN");

  const response = await fetch(`${base}${path}`, {
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
  });

  if (!response.ok) {
    throw new Error(`GET ${path} returned ${response.status}`);
  }
  return (await response.json()) as T;
}

export function listRepos(): Promise<RepoSummary[]> {
  return apiGet<RepoSummary[]>("/api/repos");
}

export function listJobs(repoId: number, limit = 50): Promise<JobRow[]> {
  return apiGet<JobRow[]>(`/api/repos/${repoId}/jobs?limit=${limit}`);
}
