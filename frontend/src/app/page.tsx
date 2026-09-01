import {
  LabelValueRow,
  SectionLabel,
  StatBlock,
  Table,
  toneClass,
  TwoToneHeading,
  type Tone,
} from "@/components/primitives";
import { listJobs, listRepos, type JobRow } from "@/lib/api";

/** Live production facts, so nothing here is prerendered at build time. */
export const dynamic = "force-dynamic";

function formatUtc(iso: string | null): string {
  if (!iso) return "—";
  return `${iso.slice(0, 10)} ${iso.slice(11, 16)} UTC`;
}

/** Table-column form: the unit moves to the header so the cell stays one line. */
function formatUtcCompact(iso: string | null): string {
  if (!iso) return "—";
  return `${iso.slice(0, 10)} ${iso.slice(11, 16)}`;
}

function formatDuration(seconds: number | null): string {
  if (seconds === null) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${Math.round(seconds % 60)}s`;
}

function conclusionTone(conclusion: string | null): Tone {
  switch (conclusion) {
    case "success":
      return "ok";
    case "failure":
      return "bad";
    case "timed_out":
      return "warn";
    default:
      return "muted";
  }
}

function jobRows(jobs: JobRow[]) {
  return jobs.map((job) => ({
    name: job.name,
    attempt: `${job.run_attempt}`,
    sha: job.head_sha.slice(0, 7),
    conclusion: (
      <span className={toneClass(conclusionTone(job.conclusion))}>
        {job.conclusion ?? "running"}
      </span>
    ),
    duration: formatDuration(job.duration_seconds),
    started: formatUtcCompact(job.started_at),
  }));
}

export default async function ReportPage() {
  const repos = await listRepos();
  const repo = repos[0];
  const jobs = repo ? await listJobs(repo.id, 50) : [];

  const executions = repos.reduce((total, item) => total + item.job_count, 0);
  const reruns = jobs.filter((job) => job.run_attempt > 1).length;
  const distinctJobNames = new Set(jobs.map((job) => job.name)).size;

  return (
    <main className="mx-auto max-w-[680px] px-6 py-24">
      <SectionLabel>report</SectionLabel>
      <TwoToneHeading
        className="mt-4"
        lead="Flaky jobs,"
        trail="found from history you already have."
      />
      <p className="mt-8 text-[15px] leading-[1.6] text-text-muted">
        Every job execution below arrived as a GitHub Actions webhook, was
        deduplicated on its delivery id, and was written to Postgres by one
        worker. Nothing was uploaded from inside a test job and no workflow file
        was edited to produce it.
      </p>

      <section className="mt-24">
        <SectionLabel>ingested</SectionLabel>
        <div className="mt-8 flex flex-wrap gap-x-24 gap-y-8">
          <StatBlock
            value={`${executions}`}
            label="job executions"
            caption={`across ${repos.length} installed ${repos.length === 1 ? "repository" : "repositories"}`}
          />
          <StatBlock
            value={`${reruns}`}
            label="re-run attempts"
            caption={`attempt 2 or later, over ${distinctJobNames} distinct job names`}
          />
        </div>
      </section>

      {repo ? (
        <>
          <section className="mt-24">
            <SectionLabel>repository</SectionLabel>
            <div className="mt-8">
              <LabelValueRow label="full name" value={repo.full_name} />
              <LabelValueRow
                label="visibility"
                value={repo.private ? "private" : "public"}
                tone="muted"
              />
              <LabelValueRow label="repo id" value={`${repo.id}`} />
              <LabelValueRow
                label="job executions"
                value={`${repo.job_count}`}
              />
              <LabelValueRow
                label="last completed job"
                value={formatUtc(repo.last_job_at)}
              />
            </div>
          </section>

          <section className="mt-24">
            <SectionLabel>recent job executions</SectionLabel>
            <div className="mt-8">
              <Table
                columns={[
                  { key: "name", header: "job" },
                  { key: "attempt", header: "att", numeric: true },
                  { key: "sha", header: "sha", numeric: true },
                  { key: "conclusion", header: "result" },
                  { key: "duration", header: "duration", numeric: true },
                  { key: "started", header: "started · utc", numeric: true },
                ]}
                rows={jobRows(jobs)}
              />
            </div>
          </section>
        </>
      ) : (
        <section className="mt-24">
          <SectionLabel>repository</SectionLabel>
          <p className="mt-8 text-[15px] leading-[1.6] text-text-muted">
            No installed repositories yet. Install the app on a repo that runs
            Actions and push to it.
          </p>
        </section>
      )}

      <section className="mt-24">
        <SectionLabel>what this does not show yet</SectionLabel>
        <p className="mt-8 text-[15px] leading-[1.6] text-text-muted">
          Detection is not wired in. These are raw job executions, not flake
          events, so a job that failed and then passed on a re-run is listed
          twice here and called flaky nowhere. Re-run recovery, same-commit
          disagreement, and the Wilson-ranked leaderboard come next. Job names
          are stored whole, matrix values included, because different matrix legs
          are different jobs.
        </p>
      </section>
    </main>
  );
}
