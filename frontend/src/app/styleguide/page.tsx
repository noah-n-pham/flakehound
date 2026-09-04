import {
  Body,
  Button,
  ExternalLink,
  Heading,
  InlineLink,
  IntervalBar,
  LabelValueRow,
  MetaStrip,
  Page,
  Rule,
  Section,
  SectionLabel,
  ShareBar,
  StatBlock,
  Switcher,
  Table,
  Timeline,
  TrendChart,
  TwoToneHeading,
} from "@/components/primitives";

/**
 * The anti-drift route. Every primitive on this page is imported from
 * components/primitives.tsx, the same module the report renders from, so a
 * component cannot exist in two versions. Extend this page rather than rebuild it.
 */
export const metadata = { title: "styleguide" };

const tokens = [
  ["--color-bg", "#0F0F0F", "bg-bg"],
  ["--color-bg-elevated", "#161616", "bg-bg-elevated"],
  ["--color-border", "#262626", "bg-border"],
  ["--color-border-strong", "#383838", "bg-border-strong"],
  ["--color-text", "#EDEDED", "bg-text"],
  ["--color-text-muted", "#8A8A8A", "bg-text-muted"],
  ["--color-text-faint", "#5A5A5A", "bg-text-faint"],
  ["--color-btn-fill", "#E8E8E8", "bg-btn-fill"],
  ["--color-btn-fill-text", "#111111", "bg-btn-fill-text"],
  ["--color-ok", "#7C9A7C", "bg-ok"],
  ["--color-warn", "#A3936A", "bg-warn"],
  ["--color-bad", "#A37C7C", "bg-bad"],
];

export default function StyleguidePage() {
  return (
    <Page>
      <SectionLabel>styleguide</SectionLabel>
      <TwoToneHeading
        className="mt-4"
        lead="Every primitive,"
        trail="in one place so there is never a second version."
      />

      <Section label="color tokens">
        <div className="grid grid-cols-1 gap-0">
          {tokens.map(([token, hex, swatch]) => (
            <div
              key={token}
              className="flex items-center gap-4 border-b border-border py-3"
            >
              <span
                className={`h-5 w-5 shrink-0 border border-border ${swatch}`}
              />
              <span className="flex-1 font-mono text-[13px] text-text-muted">
                {token}
              </span>
              <span className="font-mono text-[13px] text-text">{hex}</span>
            </div>
          ))}
        </div>
      </Section>

      <Section label="type scale">
        <p className="font-display text-[44px] leading-[48px] font-light text-text">
          display 44/48
        </p>
        <Heading className="mt-8" level={2}>
          h2 28
        </Heading>
        <Heading className="mt-8" level={3}>
          h3 20
        </Heading>
        <Body className="mt-8">
          body 15 in Inter at line-height 1.6, muted. Paragraphs and labels only.
          Every number, timestamp, SHA, and uppercase label is mono instead.
        </Body>
        <p className="mt-4 text-[13px] text-text-muted">small 13</p>
        <p className="mt-4 font-mono text-[11px] uppercase tracking-[0.12em] text-text-faint">
          mono-label 11
        </p>
      </Section>

      <Section label="typeface roles">
        <LabelValueRow label="display headings" value="Instrument Serif" />
        <LabelValueRow label="body copy and labels" value="Inter" />
        <LabelValueRow
          label="numbers, data, metadata"
          value="JetBrains Mono"
        />
      </Section>

      <Section label="two-tone heading">
        <TwoToneHeading
          lead="Flaky CI,"
          trail="found from history you already have."
        />
      </Section>

      <Section label="stat block">
        <div className="flex flex-wrap gap-x-24 gap-y-8">
          <StatBlock
            value="4.2"
            unit="%"
            label="flake rate"
            caption="wilson lower bound 2.8% over 1,204 runs"
          />
          <StatBlock value="5" label="job executions" caption="across 1 repository" />
        </div>
      </Section>

      <Section label="label / value rows">
        <LabelValueRow label="full name" value="noah-n-pham/flakehound" />
        <LabelValueRow label="visibility" value="private" tone="muted" />
        <LabelValueRow label="last completed job" value="2026-09-01 19:43 UTC" />
      </Section>

      <Section label="table">
        <Table
          columns={[
            { key: "name", header: "job" },
            { key: "attempt", header: "att", numeric: true },
            { key: "result", header: "result" },
            { key: "duration", header: "duration", numeric: true },
          ]}
          rows={[
            {
              name: "test (3.12)",
              attempt: "1",
              result: <span className="text-ok">success</span>,
              duration: "37s",
            },
            {
              name: "build and deploy",
              attempt: "1",
              result: <span className="text-bad">failure</span>,
              duration: "1m 33s",
            },
            {
              name: "build and deploy",
              attempt: "3",
              result: <span className="text-ok">success</span>,
              duration: "6m 55s",
            },
          ]}
        />
      </Section>

      <Section label="interval bar">
        <Body className="mb-8">
          A 95% Wilson interval drawn on one shared scale. The faint span is the
          interval, the bright tick is the rate itself. All three rows below are
          scaled to 40 points, so their widths mean the same thing: 3 flakes in 22
          runs is barely a narrower claim than 1 in 3, and 40 in 1,204 is a
          different kind of statement altogether.
        </Body>
        <Table
          columns={[
            { key: "job", header: "job" },
            { key: "counts", header: "flakes / opps", numeric: true },
            { key: "rate", header: "rate", numeric: true },
            { key: "interval", header: "wilson 95% · pts", numeric: true },
          ]}
          rows={[
            {
              job: "build and deploy",
              counts: "3 / 22",
              rate: "13.6%",
              interval: (
                <span className="inline-flex items-center gap-3">
                  <IntervalBar lower={0.047} upper={0.333} point={0.136} max={0.4} />
                  <span>4.7–33.3</span>
                </span>
              ),
            },
            {
              job: "e2e (chromium)",
              counts: "1 / 3",
              rate: "33.3%",
              interval: (
                <span className="inline-flex items-center gap-3">
                  <IntervalBar lower={0.061} upper={0.79} point={0.333} max={0.4} />
                  <span>6.1–79.0</span>
                </span>
              ),
            },
            {
              job: "test (ubuntu-latest, 3.11)",
              counts: "40 / 1204",
              rate: "3.3%",
              interval: (
                <span className="inline-flex items-center gap-3">
                  <IntervalBar lower={0.024} upper={0.045} point={0.033} max={0.4} />
                  <span>2.4–4.5</span>
                </span>
              ),
            },
          ]}
        />
      </Section>

      <Section label="share bar">
        <Body className="mb-8">
          The same 88px track as the interval bar, with one magnitude from zero instead
          of two ends and a point. A share has nothing to estimate, so the bar is the
          number. The scale is the largest share on the page, not 100%, or a table whose
          biggest slice is 12% would draw four bars all hugging the left edge.
        </Body>
        <Table
          columns={[
            { key: "workflow", header: "workflow" },
            { key: "total", header: "total", numeric: true },
            { key: "share", header: "share", numeric: true },
          ]}
          rows={[
            {
              workflow: "ci",
              total: "4h 12m",
              share: (
                <span className="inline-flex items-center gap-3">
                  <ShareBar value={0.62} max={0.62} />
                  <span>62.0%</span>
                </span>
              ),
            },
            {
              workflow: "e2e",
              total: "1h 58m",
              share: (
                <span className="inline-flex items-center gap-3">
                  <ShareBar value={0.29} max={0.62} />
                  <span>29.0%</span>
                </span>
              ),
            },
            {
              workflow: "deploy",
              total: "36m 20s",
              share: (
                <span className="inline-flex items-center gap-3">
                  <ShareBar value={0.09} max={0.62} />
                  <span>9.0%</span>
                </span>
              ),
            },
          ]}
        />
      </Section>

      <Section label="trend chart">
        <Body className="mb-8">
          Two per-day percentiles over a window, with no gridlines, no axis ticks and no
          legend box. The scale is stated once above the plot and the ends of the x axis
          are named. The gap in the middle is a day with no observation, drawn as a gap
          rather than bridged or read as zero. It is an inline SVG in a server component:
          thirty numbers that never change do not need a charting library in the browser.
        </Body>
        <TrendChart
          max={420}
          peakLabel="p95 peaks at 7m 0s · scale starts at zero"
          xLabels={["Aug 2", "Aug 31"]}
          series={[
            {
              name: "p95",
              emphasis: true,
              lastLabel: "5m 30s",
              values: [
                180, 200, 240, 220, 260, 300, 280, null, null, 340, 360, 320, 400, 420,
                380, 360, 340, 330, 320, 330,
              ],
            },
            {
              name: "p50",
              lastLabel: "2m 40s",
              values: [
                90, 100, 110, 105, 120, 140, 130, null, null, 150, 160, 150, 170, 180,
                170, 165, 160, 158, 155, 160,
              ],
            },
          ]}
        />
      </Section>

      <Section label="timeline">
        <Body className="mb-8">
          One job&apos;s history, oldest commit at the left. Each mark is an attempt
          and each underlined group is a commit, so the third group below, a failure
          beside a pass under one rule, is a re-run recovery, and the last is two
          runs disagreeing on the same commit. A pass is deliberately uncoloured: a
          strip of thirty green squares would say nothing thirty times.
        </Body>
        <Timeline
          commits={[
            {
              sha: "a11ce0",
              state: "passed",
              marks: [{ outcome: "success" }],
              title: "a11ce0 · passed",
            },
            {
              sha: "b0bb1e",
              state: "failed",
              marks: [{ outcome: "failure" }],
              title: "b0bb1e · failed",
            },
            {
              sha: "dead42",
              state: "flaked",
              marks: [{ outcome: "failure" }, { outcome: "success" }],
              title: "dead42 · flaked · 2 attempts",
            },
            {
              sha: "cafe11",
              state: "unjudged",
              marks: [{ outcome: null }],
              title: "cafe11 · unjudged · cancelled",
            },
            {
              sha: "fee1ed",
              state: "flaked",
              marks: [{ outcome: "success" }, { outcome: "failure" }],
              title: "fee1ed · flaked · 2 runs",
            },
          ]}
        />
      </Section>

      <Section label="switcher">
        <Switcher
          items={[
            { label: "noah-n-pham/flakehound", href: "#", current: true },
            { label: "noah-n-pham/form-check", href: "#", current: false },
          ]}
        />
      </Section>

      <Section label="status tones">
        <div className="flex gap-8 font-mono text-[13px]">
          <span className="text-ok">success</span>
          <span className="text-bad">failure</span>
          <span className="text-warn">timed_out</span>
          <span className="text-text-muted">cancelled</span>
        </div>
      </Section>

      <Section label="buttons">
        <div className="flex items-center gap-4">
          <Button>primary</Button>
          <Button variant="secondary">secondary</Button>
          <Button disabled>disabled</Button>
        </div>
        {/* The compact size exists for the 56px nav, where the default's 13px
            text and 8px padding crowd the bar. Same component, one prop. */}
        <div className="mt-6 flex items-center gap-4">
          <Button size="compact">compact primary</Button>
          <Button size="compact" variant="secondary">
            compact secondary
          </Button>
        </div>
      </Section>

      <Section label="inline link">
        <Body>
          Body copy with an <InlineLink href="/public/flaky">inline link</InlineLink>{" "}
          in it. Monochrome leaves no colour to signal a link with, so the
          underline is the affordance and hover strengthens it.
        </Body>
      </Section>

      <Section label="external link">
        {/* Two faces because the board uses both: sans in a sentence, mono wherever
            the link text is an identifier. Both open in a new tab. A reader
            checking a claim on github.com should keep the page they were reading. */}
        <Body>
          Body copy pointing at{" "}
          <ExternalLink href="https://github.com/angr/cle">
            somebody else&apos;s repository
          </ExternalLink>
          , which is what the public board does on every row.
        </Body>
        <p className="mt-6 font-mono text-[13px] text-text-muted">
          run{" "}
          <ExternalLink
            href="https://github.com/angr/cle/actions/runs/33774557574"
            mono
          >
            33774557574
          </ExternalLink>{" "}
          · attempt 1
        </p>
      </Section>

      <Section label="rule">
        <Rule />
      </Section>

      <Section label="section">
        <Body>
          This block, and every block above it, is the section primitive: 96px of
          rhythm above the label, 32px between the label and its content. The
          label is the only argument.
        </Body>
      </Section>

      <Section label="layout">
        <LabelValueRow label="content column" value="max-w-[680px] centered" />
        <LabelValueRow
          label="wide data tables"
          value="max-w-[960px]"
          tone="muted"
        />
        <LabelValueRow label="between major sections" value="96px" />
        <LabelValueRow label="between blocks" value="32px" />
        <LabelValueRow label="inside a block" value="16px" />
        <LabelValueRow label="nav height" value="56px" />
        <LabelValueRow label="border radius" value="2px or 0" />
      </Section>

      <Section label="motion">
        <Body>
          150ms opacity fades and nothing else: no slides, no springs, no
          scroll-triggered animation. Hover the buttons above to see the whole
          motion system.
        </Body>
      </Section>

      <Section label="metadata strip">
        <MetaStrip
          items={[
            "github actions webhooks",
            "deduplicated on delivery id",
            "wilson 95% ci",
          ]}
        />
      </Section>

      <Section label="shell">
        <LabelValueRow label="nav" value="on every page, 56px" />
        <LabelValueRow label="footer" value="hairline + metadata strip" />
        <LabelValueRow label="404" value="/nonexistent" tone="muted" />
        <LabelValueRow
          label="error boundary"
          value="retry, no message"
          tone="muted"
        />
        <LabelValueRow label="title template" value="%s · flakehound" />
      </Section>
    </Page>
  );
}
