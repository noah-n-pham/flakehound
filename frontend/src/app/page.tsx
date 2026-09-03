import { notFound } from "next/navigation";

import { auth } from "@/auth";
import {
  Body,
  Heading,
  InlineLink,
  IntervalBar,
  LabelValueRow,
  MetaStrip,
  Page,
  Section,
  SectionLabel,
  StatBlock,
  Switcher,
  Table,
  Timeline,
  toneClass,
  TwoToneHeading,
  type TimelineCommit,
  type Tone,
} from "@/components/primitives";
import {
  listFlaky,
  listJobHistory,
  listJobs,
  listRepos,
  type CommitHistory,
  type FlakyRow,
  type JobRow,
  type RepoSummary,
} from "@/lib/api";
import {
  formatDay,
  formatDuration,
  formatPercent,
  formatPoints,
  formatUtc,
  formatUtcCompact,
} from "@/lib/format";
import { authorizedRepoIds } from "@/lib/github";

/** Live production facts, so nothing here is prerendered at build time. */
export const dynamic = "force-dynamic";

const WINDOW_DAYS = 30;
const LIMIT = 50;
/** Commits on the timeline, not attempts — a heavily re-run commit is one group. */
const COMMITS = 40;

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

/**
 * One scale for every bar in the table, so their widths are comparable. The widest
 * upper bound on the page sets it, with a floor for a repo whose intervals are all
 * tiny — a 0.4pt scale would draw three indistinguishable full-width bars.
 */
function intervalScale(board: FlakyRow[]): number {
  return Math.max(...board.map((row) => row.wilson_upper ?? 0), 0.05);
}

function boardRows(board: FlakyRow[]) {
  const scale = intervalScale(board);
  return board.map((row) => ({
    job: row.job_name,
    opportunities: `${row.opportunities}`,
    failures: `${row.failures}`,
    flakes: `${row.flakes}`,
    rate: formatPercent(row.flake_rate),
    interval:
      row.wilson_lower === null || row.wilson_upper === null ? (
        "—"
      ) : (
        <span className="inline-flex items-center gap-3">
          <IntervalBar
            lower={row.wilson_lower}
            upper={row.wilson_upper}
            point={row.flake_rate}
            max={scale}
          />
          <span>{formatPoints(row.wilson_lower, row.wilson_upper)}</span>
        </span>
      ),
    last: formatDay(row.last_flake_at),
  }));
}

function stateTone(state: CommitHistory["state"]): Tone {
  switch (state) {
    case "flaked":
      return "warn";
    case "failed":
      return "bad";
    case "passed":
      return "ok";
    default:
      return "muted";
  }
}

/**
 * The API returns the newest commit first, because a caller asking for ten wants the
 * ten most recent. A timeline reads left to right in time, so it is reversed here —
 * once, at the boundary, rather than by having the API answer in an order that would
 * make `limit` mean the ten oldest.
 */
function timelineCommits(history: CommitHistory[]): TimelineCommit[] {
  return [...history].reverse().map((commit) => ({
    sha: commit.head_sha,
    state: commit.state,
    marks: commit.attempts.map((attempt) => ({ outcome: attempt.outcome })),
    title: [
      commit.head_sha.slice(0, 7),
      commit.state,
      `${commit.attempts.length} ${commit.attempts.length === 1 ? "attempt" : "attempts"}`,
      `${commit.runs} ${commit.runs === 1 ? "run" : "runs"}`,
      formatUtcCompact(commit.last_completed_at),
    ].join(" · "),
  }));
}

function commitRows(history: CommitHistory[]) {
  return history.map((commit) => ({
    sha: commit.head_sha.slice(0, 7),
    state: (
      <span className={toneClass(stateTone(commit.state))}>{commit.state}</span>
    ),
    attempts: `${commit.attempts.length}`,
    runs: `${commit.runs}`,
    opportunities: `${commit.opportunities}`,
    flakes: `${commit.flakes}`,
    last: formatUtcCompact(commit.last_completed_at),
  }));
}

/** Signed out. No repository data is fetched on this path at all. */
function SignedOut() {
  return (
    <Page>
      <SectionLabel>report</SectionLabel>
      <TwoToneHeading
        className="mt-4"
        lead="Flaky CI,"
        trail="found from history you already have."
      />
      <Body className="mt-8">
        Sign in with GitHub to see the repositories you have installed Flakehound
        on, ranked by how much of their failure is noise. Which repositories you
        can read is resolved from GitHub itself on every session, so this page can
        only ever show you what GitHub already says is yours.
      </Body>
      <Body className="mt-8">
        The <InlineLink href="/public/flaky">public board</InlineLink> needs no
        account.
      </Body>
    </Page>
  );
}

function NothingInstalled() {
  return (
    <Section label="repositories">
      <Body>
        No installed repositories yet. Install the app on a repo that runs Actions
        and push to it. If you have just installed it, this page re-resolves your
        repositories from GitHub every five minutes.
      </Body>
    </Section>
  );
}

export default async function ReportPage({
  searchParams,
}: {
  searchParams: Promise<{ repo?: string; job?: string }>;
}) {
  const session = await auth();
  const repoIds = session?.user ? await authorizedRepoIds() : null;
  if (!session?.user || repoIds === null) {
    return <SignedOut />;
  }

  const repos = await listRepos(repoIds);
  const requested = await searchParams;
  const repo = pick(repos, requested.repo);

  if (!repo) {
    return (
      <Page>
        <Header />
        <NothingInstalled />
      </Page>
    );
  }

  const [board, jobs] = await Promise.all([
    listFlaky(repo.id, repoIds, WINDOW_DAYS, LIMIT),
    listJobs(repo.id, repoIds, LIMIT),
  ]);

  // The timeline is about one job, and which job is only knowable once the board
  // has answered — the default is the one ranked worst.
  const tracked = pickJob(board, requested.job);
  const history = tracked
    ? await listJobHistory(
        repo.id,
        tracked.job_name,
        repoIds,
        tracked.workflow_id,
        WINDOW_DAYS,
        COMMITS,
      )
    : [];

  const flakes = board.reduce((total, row) => total + row.flakes, 0);
  const opportunities = board.reduce((total, row) => total + row.opportunities, 0);
  const worst = board[0];

  return (
    <Page wide>
      <Header />
      <Body className="mt-8 max-w-[680px]">
        Every number here was derived from GitHub Actions webhooks — deduplicated
        on delivery id, written to Postgres by one worker, then swept into a daily
        rollup. Nothing was uploaded from inside a test job and no workflow file
        was edited to produce it.
      </Body>

      {repos.length > 1 ? (
        <div className="mt-8">
          <Switcher
            items={repos.map((item) => ({
              label: item.full_name,
              // Deliberately drops `?job=`: a job name belongs to the repo it ran in.
              href: reportHref(item, repos),
              current: item.id === repo.id,
            }))}
          />
        </div>
      ) : null}

      <Section label={`last ${WINDOW_DAYS} days`}>
        <div className="flex flex-wrap gap-x-24 gap-y-8">
          {worst ? (
            <StatBlock
              value={(100 * (worst.flake_rate ?? 0)).toFixed(1)}
              unit="%"
              label={`flake rate · ${worst.job_name}`}
              caption={`wilson lower bound ${formatPercent(worst.wilson_lower)} over ${worst.opportunities} opportunities`}
            />
          ) : null}
          <StatBlock
            value={`${flakes}`}
            label="flakes detected"
            caption={`over ${opportunities} opportunities in ${board.length} ${board.length === 1 ? "job" : "jobs"}`}
          />
        </div>
      </Section>

      {board.length > 0 ? (
        <Section label="ranked by wilson lower bound">
          <Table
            columns={[
              { key: "job", header: "job" },
              { key: "opportunities", header: "opps", numeric: true },
              { key: "failures", header: "fails", numeric: true },
              { key: "flakes", header: "flakes", numeric: true },
              { key: "rate", header: "rate", numeric: true },
              { key: "interval", header: "wilson 95% · pts", numeric: true },
              { key: "last", header: "last flake", numeric: true },
            ]}
            rows={boardRows(board)}
          />
        </Section>
      ) : (
        <Section label="ranked by wilson lower bound">
          <Body className="max-w-[680px]">
            Nothing ranked yet. A job appears here once it has had at least one
            opportunity to flake in the window — a completed run whose failure
            could be judged — and the rollup that feeds this table is swept once a
            minute, so a repository installed in the last few seconds is empty
            rather than broken.
          </Body>
        </Section>
      )}

      {tracked ? (
        <Section label="history by commit">
          <Switcher
            items={board.map((row) => ({
              label: row.job_name,
              href: reportHref(repo, repos, row.job_name),
              current: row.job_name === tracked.job_name,
            }))}
          />
          <Heading level={3} className="mt-8">
            {tracked.job_name}
          </Heading>
          {history.length > 0 ? (
            <>
              <div className="mt-8">
                <Timeline commits={timelineCommits(history)} />
              </div>
              <div className="mt-8">
                <MetaStrip
                  items={[
                    `${history.length} ${history.length === 1 ? "commit" : "commits"}`,
                    `${history.filter((commit) => commit.state === "flaked").length} flaked`,
                    `${history.filter((commit) => commit.state === "failed").length} failed outright`,
                    `${history.reduce((total, commit) => total + commit.attempts.length, 0)} attempts`,
                  ]}
                />
              </div>
              <div className="mt-8">
                <Table
                  columns={[
                    { key: "sha", header: "commit" },
                    { key: "state", header: "state" },
                    { key: "attempts", header: "att", numeric: true },
                    { key: "runs", header: "runs", numeric: true },
                    { key: "opportunities", header: "opps", numeric: true },
                    { key: "flakes", header: "flakes", numeric: true },
                    { key: "last", header: "last finished · utc", numeric: true },
                  ]}
                  rows={commitRows(history)}
                />
              </div>
            </>
          ) : (
            <Body className="mt-8 max-w-[680px]">
              Nothing in the last {WINDOW_DAYS} days. The leaderboard reads a rollup
              of whole UTC days and this reads the job rows themselves, so a job
              whose only runs are older than the window can rank above and be empty
              here.
            </Body>
          )}
        </Section>
      ) : null}

      <Section label="repository">
        <LabelValueRow label="full name" value={repo.full_name} />
        <LabelValueRow
          label="visibility"
          value={repo.private ? "private" : "public"}
          tone="muted"
        />
        <LabelValueRow label="repo id" value={`${repo.id}`} />
        <LabelValueRow label="job executions" value={`${repo.job_count}`} />
        <LabelValueRow
          label="last completed job"
          value={formatUtc(repo.last_job_at)}
        />
      </Section>

      <Section label="recent job executions">
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
      </Section>

      <Section label="how the ranking works">
        <Body className="max-w-[680px]">
          A flake is a job that failed and then passed with the commit unchanged —
          either on a re-run of the same run, or against a sibling job on the same
          commit. An opportunity is a completed job whose result could be judged
          that way at all, which is why the denominator is smaller than the
          repository&apos;s job execution count. The rate is flakes over
          opportunities, and the
          interval beside it is the 95% Wilson score interval on that rate; ranking
          uses its lower bound, so a job that flaked once in three runs does not
          outrank one that flaked forty times in a thousand.
        </Body>
        <Body className="mt-8 max-w-[680px]">
          Two limits worth stating. Job names are stored whole, matrix values
          included, because two matrix legs are two different jobs that fail for
          different reasons. And a re-run that passes because somebody fixed
          something outside the repository looks exactly like a flake from the
          Actions API — the commit is identical either way — so a repository whose
          workflow depends on external state will see a rate that is too high
          rather than too low.
        </Body>
      </Section>
    </Page>
  );
}

function Header() {
  return (
    <>
      <SectionLabel>report</SectionLabel>
      <TwoToneHeading
        className="mt-4"
        lead="Flaky CI,"
        trail="found from history you already have."
      />
    </>
  );
}

/**
 * Which repo the page is about. `repos` came back from the API already filtered to
 * the caller's authorized set, so matching against it is the check — a repo id in the
 * query string is a client-supplied value and is never passed through on its own.
 * An id that survives the filter but names nothing is a 404, the same answer the API
 * gives for an unauthorized repo, so the query string cannot be used to learn which
 * repositories exist.
 */
function pick(repos: RepoSummary[], requested: string | undefined): RepoSummary | undefined {
  if (requested === undefined) return repos[0];
  const match = repos.find((repo) => `${repo.id}` === requested);
  if (!match) notFound();
  return match;
}

/**
 * Which job the timeline is about. Checked against the board the API already
 * returned for a repo the caller is authorized on, for the same reason `?repo=` is
 * checked against the repo list: a query-string value is never passed into a read on
 * its own. A name that is not on the board is a 404 rather than an empty timeline,
 * so the query string cannot be used to ask whether a job exists.
 */
function pickJob(board: FlakyRow[], requested: string | undefined): FlakyRow | undefined {
  if (requested === undefined) return board[0];
  const match = board.find((row) => row.job_name === requested);
  if (!match) notFound();
  return match;
}

/** The canonical URL for one view of the report. Defaults stay out of the query. */
function reportHref(repo: RepoSummary, repos: RepoSummary[], jobName?: string): string {
  const query = new URLSearchParams();
  if (repo.id !== repos[0].id) query.set("repo", `${repo.id}`);
  if (jobName !== undefined) query.set("job", jobName);
  const search = query.toString();
  return search ? `/?${search}` : "/";
}
