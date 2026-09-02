import { notFound } from "next/navigation";

import { auth } from "@/auth";
import {
  Body,
  InlineLink,
  IntervalBar,
  LabelValueRow,
  Page,
  Section,
  SectionLabel,
  StatBlock,
  Switcher,
  Table,
  toneClass,
  TwoToneHeading,
  type Tone,
} from "@/components/primitives";
import {
  listFlaky,
  listJobs,
  listRepos,
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

/**
 * Signed out. Until Section E this page served a private repository's CI metadata to
 * anyone with the URL — accepted deliberately while auth was being built (H-004b),
 * and closed there.
 */
function SignedOut() {
  return (
    <Page>
      <SectionLabel>report</SectionLabel>
      <TwoToneHeading
        className="mt-4"
        lead="Flaky jobs,"
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
  searchParams: Promise<{ repo?: string }>;
}) {
  const session = await auth();
  const repoIds = session?.user ? await authorizedRepoIds() : null;
  if (!session?.user || repoIds === null) {
    return <SignedOut />;
  }

  const repos = await listRepos(repoIds);
  const requested = (await searchParams).repo;
  const repo = pick(repos, requested);

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
              href: item.id === repos[0].id ? "/" : `/?repo=${item.id}`,
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
        lead="Flaky jobs,"
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
