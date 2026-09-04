/**
 * The BFF's only door to FastAPI. Server-side only: the internal bearer token
 * lives in this process and never reaches the browser, which is why neither
 * variable is NEXT_PUBLIC_. A missing variable throws. A dashboard that
 * silently renders nothing looks the same as a healthy empty account.
 *
 * Every read also carries `X-Authorized-Repo-Ids`, the set resolved in
 * `lib/github.ts` from GitHub's installations API. It is a **required argument**
 * rather than something looked up in here, because the caller is the only one that
 * knows whether anybody is signed in, and the API answers 400 rather than
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

/**
 * One ranked job. Both bounds and the point estimate arrive from the API, which is
 * the only place the Wilson interval is computed. The width of the interval is what
 * says how much the rate is worth believing, so it travels with it.
 */
export type FlakyRow = {
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

/** One attempt on one commit. `outcome` is null when it was not an opportunity. */
export type HistoryAttempt = {
  job_id: number;
  run_id: number;
  run_attempt: number;
  workflow_id: number | null;
  conclusion: string | null;
  outcome: string | null;
  implicated: boolean;
  started_at: string | null;
  completed_at: string | null;
  duration_seconds: number | null;
};

/**
 * One commit's worth of one job. `state` is derived by the API from the attempts
 * (`flaked` beats `failed` beats `passed`) and the page draws it rather than
 * re-deciding it, for the same reason it never recomputes a Wilson bound.
 */
export type CommitHistory = {
  head_sha: string;
  state: "flaked" | "failed" | "passed" | "unjudged";
  runs: number;
  opportunities: number;
  failures: number;
  flakes: number;
  first_started_at: string | null;
  last_completed_at: string | null;
  attempts: HistoryAttempt[];
};

/**
 * One slice of a repo's Actions time. `job_name` is null when grouping by workflow.
 *
 * `seconds` is wall clock and **not a bill**. GitHub rounds each job up to the minute
 * and multiplies by a runner factor, neither of which the Actions API exposes. `share`
 * is computed by the API so two pages cannot disagree about the denominator.
 */
export type MinutesRow = {
  workflow_id: number | null;
  workflow_name: string | null;
  job_name: string | null;
  runs: number;
  seconds: number;
  share: number;
  mean_seconds: number | null;
};

/** One day of one job's duration. A day it did not run is absent, not zero. */
export type DurationPoint = {
  day: string;
  workflow_id: number | null;
  runs: number;
  p50_seconds: number | null;
  p95_seconds: number | null;
  total_seconds: number;
};

/**
 * The one failing job run behind a board row, chosen by the API out of the same
 * evidence its `flakes` were counted through. Null only when the row never flaked.
 */
export type FlakeProof = {
  job_id: number;
  run_id: number;
  run_attempt: number;
  head_sha: string;
  conclusion: string | null;
  completed_at: string | null;
};

/**
 * The same row on the public board, which spans repos and so must name one, and,
 * because it publishes a claim about somebody else's repository, must carry both the
 * workflow the job belongs to and a job run that proves the claim.
 *
 * `workflow_name` is usually null for a repo we only observe: a webhook carries the
 * workflow's name and the runs listing carries only its path.
 */
export type PublicFlakyRow = FlakyRow & {
  repo_id: number;
  repo_full_name: string;
  workflow_name: string | null;
  workflow_path: string | null;
  proof: FlakeProof | null;
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
 * One repo's leaderboard, ranked by the Wilson interval's lower bound rather than by
 * the rate. Served from the daily rollup, so the window is a whole number of UTC days.
 */
export function listFlaky(
  repoId: number,
  authorizedRepoIds: number[],
  windowDays = 30,
  limit = 50,
): Promise<FlakyRow[]> {
  return apiGet<FlakyRow[]>(
    `/api/repos/${repoId}/flaky?window_days=${windowDays}&limit=${limit}`,
    authorizedRepoIds,
  );
}

/**
 * One job's timeline, newest commit first. `limit` counts commits, not attempts.
 *
 * The name is a path segment because that is how the API addresses it, and job names
 * are stored whole (matrix values, spaces, brackets and all), so it is encoded here
 * rather than trusted to survive as typed. `workflowId` narrows the name to one
 * workflow, since two workflows can run a job of the same name and they are not the
 * same job.
 */
export function listJobHistory(
  repoId: number,
  jobName: string,
  authorizedRepoIds: number[],
  workflowId: number | null = null,
  windowDays = 30,
  limit = 30,
): Promise<CommitHistory[]> {
  const query = new URLSearchParams({
    window_days: `${windowDays}`,
    limit: `${limit}`,
  });
  if (workflowId !== null) query.set("workflow_id", `${workflowId}`);
  return apiGet<CommitHistory[]>(
    `/api/repos/${repoId}/jobs/${encodeURIComponent(jobName)}/history?${query}`,
    authorizedRepoIds,
  );
}

/**
 * Where the repo's Actions time went, biggest consumer first. `group_by=job` groups on
 * the workflow too, because a job name is only unique inside its workflow.
 */
export function listMinutes(
  repoId: number,
  authorizedRepoIds: number[],
  groupBy: "workflow" | "job" = "workflow",
  windowDays = 30,
  limit = 50,
): Promise<MinutesRow[]> {
  return apiGet<MinutesRow[]>(
    `/api/repos/${repoId}/minutes?group_by=${groupBy}&window_days=${windowDays}&limit=${limit}`,
    authorizedRepoIds,
  );
}

/**
 * One job's p50 and p95 per UTC day, oldest first. The series has gaps: percentiles
 * cannot be re-aggregated, so the API returns the rollup's days verbatim rather than
 * inventing a value for a day the job did not run.
 */
export function listDurationTrend(
  repoId: number,
  jobName: string,
  authorizedRepoIds: number[],
  workflowId: number | null = null,
  windowDays = 30,
): Promise<DurationPoint[]> {
  const query = new URLSearchParams({ window_days: `${windowDays}` });
  if (workflowId !== null) query.set("workflow_id", `${workflowId}`);
  return apiGet<DurationPoint[]>(
    `/api/repos/${repoId}/jobs/${encodeURIComponent(jobName)}/duration?${query}`,
    authorizedRepoIds,
  );
}

/**
 * The public board, and the one read that carries **no credentials at all**: not the
 * bearer token and not an authorized-repo header. That is deliberate rather than
 * economical: the endpoint takes no repo id and filters `private = false` in its own
 * SQL, so there is nothing for a header to widen, and sending one anyway would suggest
 * the boundary lives in the caller. It still goes through the BFF, because the browser
 * never calls FastAPI directly.
 */
export async function listPublicFlaky(
  windowDays = 30,
  limit = 50,
  minFlakes = 0,
): Promise<PublicFlakyRow[]> {
  const base = required("API_BASE_URL").replace(/\/$/, "");
  const query = new URLSearchParams({
    window_days: `${windowDays}`,
    limit: `${limit}`,
    min_flakes: `${minFlakes}`,
  });
  const response = await fetch(`${base}/public/flaky?${query}`, {
    cache: "no-store",
  });

  if (!response.ok) {
    throw new Error(`GET /public/flaky returned ${response.status}`);
  }
  return (await response.json()) as PublicFlakyRow[];
}
