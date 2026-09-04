/**
 * github.com URLs, built in one place.
 *
 * The API deliberately sends identifiers rather than links (a repo full name, a run
 * id, a job id), so the shape of a github.com URL is presentation and lives here. One
 * module because the public board makes a claim about a repository whose owner never
 * asked to be measured: a link that 404s is worse than no link, and two copies of this
 * pattern would eventually disagree about which one is right.
 */

const GITHUB = "https://github.com";

export function repoUrl(fullName: string): string {
  return `${GITHUB}/${fullName}`;
}

/**
 * One job run's log page. Addressed by job id rather than by run and attempt, which is
 * what makes it work for the earlier attempts of a re-run: the attempt's own page is
 * reachable, but the job id resolves to the failure itself, which is the thing being
 * pointed at.
 */
export function jobUrl(fullName: string, runId: number, jobId: number): string {
  return `${GITHUB}/${fullName}/actions/runs/${runId}/job/${jobId}`;
}

/** One commit, for a row proved by a disagreement between runs of the same SHA. */
export function commitUrl(fullName: string, sha: string): string {
  return `${GITHUB}/${fullName}/commit/${sha}`;
}
