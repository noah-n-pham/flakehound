import {
  Body,
  ExternalLink,
  InlineLink,
  IntervalBar,
  Page,
  Section,
  SectionLabel,
  StatBlock,
  Table,
  TwoToneHeading,
} from "@/components/primitives";
import { listPublicFlaky, type PublicFlakyRow } from "@/lib/api";
import { formatDay, formatPercent, formatPoints, workflowLabel } from "@/lib/format";
import { jobUrl, repoUrl } from "@/lib/links";

/** Live rows out of the rollup, so nothing here is prerendered at build time. */
export const dynamic = "force-dynamic";

export const metadata = { title: "public board" };

const WINDOW_DAYS = 30;
const LIMIT = 10;
/**
 * A board of the flakiest jobs must not contain a job that never flaked, and the
 * Wilson lower bound cannot be asked to exclude one: at zero flakes it is zero in
 * arithmetic but occasionally 3.5e-18 in floating point. So the count is the filter,
 * and the API takes it as an argument rather than assuming it.
 */
const MIN_FLAKES = 1;

/** Shared by every bar in the table, because bars on different scales compare nothing. */
function intervalScale(board: PublicFlakyRow[]): number {
  return Math.max(...board.map((row) => row.wilson_upper ?? 0), 0.05);
}

function boardRows(board: PublicFlakyRow[]) {
  const scale = intervalScale(board);
  return board.map((row, index) => ({
    rank: `${index + 1}`,
    repo: (
      <ExternalLink href={repoUrl(row.repo_full_name)}>
        {row.repo_full_name}
      </ExternalLink>
    ),
    workflow: (
      <span className="font-mono text-[12px] text-text-faint">
        {workflowLabel(row.workflow_name, row.workflow_path)}
      </span>
    ),
    job: row.job_name,
    opportunities: `${row.opportunities}`,
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
    // The row's claim, checkable in one click. Every ranked row has one, because a
    // row without flakes is not on this board.
    proof: row.proof ? (
      <ExternalLink
        href={jobUrl(row.repo_full_name, row.proof.run_id, row.proof.job_id)}
        mono
      >
        {row.proof.run_id}
        {row.proof.run_attempt > 1 ? ` · attempt ${row.proof.run_attempt}` : ""}
      </ExternalLink>
    ) : (
      "—"
    ),
    last: formatDay(row.last_flake_at),
  }));
}

export default async function PublicBoardPage() {
  const board = await listPublicFlaky(WINDOW_DAYS, LIMIT, MIN_FLAKES);
  const repos = new Set(board.map((row) => row.repo_full_name)).size;

  return (
    <Page wide>
      <SectionLabel>public board</SectionLabel>
      <TwoToneHeading
        className="mt-4"
        lead="The flakiest CI,"
        trail="ranked by what the evidence supports."
      />
      <Body className="mt-8 max-w-[680px]">
        Ten jobs from public repositories, read out of the GitHub Actions history
        those projects already publish, over the last {WINDOW_DAYS} days.{" "}
        <strong className="font-normal text-text">
          Appearing here is not adoption.
        </strong>{" "}
        None of these projects installed anything, sent us data, or asked to be
        measured — they were selected against criteria fixed before the first
        repository was read, and a flaky job says nothing about the people who
        wrote it beyond that their CI is public.
      </Body>
      <Body className="mt-6 max-w-[680px]">
        Every row links to the repository and to the failing job run behind it, so
        nothing on this page has to be taken on trust. Private repositories are
        excluded by the query itself rather than by anything on this side of it,
        and no account is needed to read any of it. Your own repositories are on
        the <InlineLink href="/">report</InlineLink>, which does need one.
      </Body>

      {board.length > 0 ? (
        <>
          <Section label="board">
            <div className="flex flex-wrap gap-x-24 gap-y-8">
              <StatBlock
                value={`${board.length}`}
                label="ranked jobs"
                caption={`across ${repos} public ${repos === 1 ? "repository" : "repositories"}`}
              />
              <StatBlock
                value={`${board.reduce((total, row) => total + row.flakes, 0)}`}
                label="flakes detected"
                caption={`over ${board.reduce((total, row) => total + row.opportunities, 0)} opportunities`}
              />
            </div>
          </Section>

          <Section label="ranked by wilson lower bound">
            <Table
              columns={[
                { key: "rank", header: "#", numeric: true },
                { key: "repo", header: "repository" },
                { key: "workflow", header: "workflow" },
                { key: "job", header: "job" },
                { key: "opportunities", header: "opps", numeric: true },
                { key: "flakes", header: "flakes", numeric: true },
                { key: "rate", header: "rate", numeric: true },
                { key: "interval", header: "wilson 95% · pts", numeric: true },
                { key: "proof", header: "proving run", numeric: true },
                { key: "last", header: "last flake", numeric: true },
              ]}
              rows={boardRows(board)}
            />
          </Section>
        </>
      ) : (
        <Section label="board">
          <Body className="max-w-[680px]">
            Nothing to rank yet. A repository reaches this board only after its
            history has been read and a signal has actually fired, so an empty
            board means no public repository has flaked inside the window — not
            that nothing was looked at.
          </Body>
        </Section>
      )}

      <Section label="how the ranking works">
        <Body className="max-w-[680px]">
          A flake is a job that failed and then passed with the commit unchanged —
          either on a re-run, or against a sibling job on the same commit. The
          rate is flakes over opportunities, and the interval beside it is the
          95% Wilson score interval on that rate. Ranking uses the interval&apos;s
          lower bound rather than the rate, which matters more on a board that
          spans repositories than on one that does not: one flake in two runs is
          a 50% rate on almost no evidence, and sorting by rate would put it
          above a job that has flaked forty times in a thousand.
        </Body>
        <Body className="mt-6 max-w-[680px]">
          The proving run is one of the job runs the row&apos;s own flake count was
          computed from, picked as the most recent failure rather than the worst
          one. A job name is only unique inside its workflow, so the workflow
          column is part of the row&apos;s identity and not decoration — two rows
          naming the same job in the same repository are two different jobs.
        </Body>
      </Section>
    </Page>
  );
}
