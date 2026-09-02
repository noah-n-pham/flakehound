import {
  Body,
  Button,
  Heading,
  InlineLink,
  LabelValueRow,
  MetaStrip,
  Page,
  Rule,
  Section,
  SectionLabel,
  StatBlock,
  Table,
  TwoToneHeading,
} from "@/components/primitives";

/**
 * The anti-drift route. Every primitive on this page is imported from
 * components/primitives.tsx — the same module the report renders from — so a
 * component cannot exist in two versions. Section F extends this; it does not
 * rebuild it.
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
          body 15 in Inter at line-height 1.6, muted. Paragraphs and labels only
          — every number, timestamp, SHA, and uppercase label is mono instead.
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
          lead="Flaky jobs,"
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
          150ms opacity fades and nothing else — no slides, no springs, no
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
        <LabelValueRow label="title template" value="%s — flakehound" />
      </Section>
    </Page>
  );
}
