import {
  Body,
  InlineLink,
  Section,
  SectionLabel,
  StatBlock,
  Table,
  TwoToneHeading,
} from "@/components/primitives";
import { listPublicFlaky, type PublicFlakyRow } from "@/lib/api";

/** Live rows out of the rollup, so nothing here is prerendered at build time. */
export const dynamic = "force-dynamic";

export const metadata = { title: "public board — flakehound" };

const WINDOW_DAYS = 30;
const LIMIT = 50;

function formatPercent(value: number | null): string {
  if (value === null) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

/** The interval as a range in points, because the column header carries the unit. */
function formatInterval(row: PublicFlakyRow): string {
  if (row.wilson_lower === null || row.wilson_upper === null) return "—";
  return `${(row.wilson_lower * 100).toFixed(1)}–${(row.wilson_upper * 100).toFixed(1)}`;
}

function formatDay(iso: string | null): string {
  return iso ? iso.slice(0, 10) : "—";
}

function boardRows(board: PublicFlakyRow[]) {
  return board.map((row) => ({
    repo: row.repo_full_name,
    job: row.job_name,
    opportunities: `${row.opportunities}`,
    flakes: `${row.flakes}`,
    rate: formatPercent(row.flake_rate),
    interval: formatInterval(row),
    last: formatDay(row.last_flake_at),
  }));
}

export default async function PublicBoardPage() {
  const board = await listPublicFlaky(WINDOW_DAYS, LIMIT);
  const repos = new Set(board.map((row) => row.repo_full_name)).size;

  return (
    <main className="mx-auto max-w-[960px] px-6 py-24">
      <SectionLabel>public board</SectionLabel>
      <TwoToneHeading
        className="mt-4"
        lead="The flakiest CI,"
        trail="ranked by what the evidence supports."
      />
      <Body className="mt-8 max-w-[680px]">
        Every public repository that has installed Flakehound, over the last{" "}
        {WINDOW_DAYS} days. No account is needed to read this page, and private
        repositories are excluded by the query itself rather than by anything on
        this side of it. Your own repositories are on the{" "}
        <InlineLink href="/">report</InlineLink>, which does need an account.
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
                { key: "repo", header: "repository" },
                { key: "job", header: "job" },
                { key: "opportunities", header: "opps", numeric: true },
                { key: "flakes", header: "flakes", numeric: true },
                { key: "rate", header: "rate", numeric: true },
                { key: "interval", header: "wilson 95% · pts", numeric: true },
                { key: "last", header: "last flake", numeric: true },
              ]}
              rows={boardRows(board)}
            />
          </Section>
        </>
      ) : (
        <Section label="board">
          <Body className="max-w-[680px]">
            Nothing to rank yet. The repositories that have installed Flakehound
            so far are private, and this page will not show them — so it is empty
            for the reason it should be, rather than because detection found
            nothing.
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
      </Section>
    </main>
  );
}
