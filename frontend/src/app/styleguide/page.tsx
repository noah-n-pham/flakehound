import {
  Button,
  LabelValueRow,
  Rule,
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
export const metadata = { title: "styleguide — flakehound" };

const tokens = [
  ["--color-bg", "#0F0F0F", "bg-bg"],
  ["--color-bg-elevated", "#161616", "bg-bg-elevated"],
  ["--color-border", "#262626", "bg-border"],
  ["--color-border-strong", "#383838", "bg-border-strong"],
  ["--color-text", "#EDEDED", "bg-text"],
  ["--color-text-muted", "#8A8A8A", "bg-text-muted"],
  ["--color-text-faint", "#5A5A5A", "bg-text-faint"],
  ["--color-btn-fill", "#E8E8E8", "bg-btn-fill"],
  ["--color-ok", "#7C9A7C", "bg-ok"],
  ["--color-warn", "#A3936A", "bg-warn"],
  ["--color-bad", "#A37C7C", "bg-bad"],
];

function Block({
  name,
  children,
}: {
  name: string;
  children: React.ReactNode;
}) {
  return (
    <section className="mt-24">
      <SectionLabel>{name}</SectionLabel>
      <div className="mt-8">{children}</div>
    </section>
  );
}

export default function StyleguidePage() {
  return (
    <main className="mx-auto max-w-[680px] px-6 py-24">
      <SectionLabel>styleguide</SectionLabel>
      <TwoToneHeading
        className="mt-4"
        lead="Every primitive,"
        trail="in one place so there is never a second version."
      />

      <Block name="color tokens">
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
      </Block>

      <Block name="type scale">
        <p className="font-display text-[44px] leading-[48px] font-light text-text">
          display 44/48
        </p>
        <p className="mt-8 font-display text-[28px] font-light text-text">
          h2 28
        </p>
        <p className="mt-8 font-display text-[20px] font-light text-text">
          h3 20
        </p>
        <p className="mt-8 text-[15px] leading-[1.6] text-text-muted">
          body 15 in Inter at line-height 1.6, muted. Paragraphs and labels only
          — every number, timestamp, SHA, and uppercase label is mono instead.
        </p>
        <p className="mt-4 text-[13px] text-text-muted">small 13</p>
        <p className="mt-4 font-mono text-[11px] uppercase tracking-[0.12em] text-text-faint">
          mono-label 11
        </p>
      </Block>

      <Block name="two-tone heading">
        <TwoToneHeading
          lead="Flaky jobs,"
          trail="found from history you already have."
        />
      </Block>

      <Block name="stat block">
        <div className="flex flex-wrap gap-x-24 gap-y-8">
          <StatBlock
            value="4.2"
            unit="%"
            label="flake rate"
            caption="wilson lower bound 2.8% over 1,204 runs"
          />
          <StatBlock value="5" label="job executions" caption="across 1 repository" />
        </div>
      </Block>

      <Block name="label / value rows">
        <LabelValueRow label="full name" value="noah-n-pham/flakehound" />
        <LabelValueRow label="visibility" value="private" tone="muted" />
        <LabelValueRow label="last completed job" value="2026-09-01 19:43 UTC" />
      </Block>

      <Block name="table">
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
      </Block>

      <Block name="status tones">
        <div className="flex gap-8 font-mono text-[13px]">
          <span className="text-ok">success</span>
          <span className="text-bad">failure</span>
          <span className="text-warn">timed_out</span>
          <span className="text-text-muted">cancelled</span>
        </div>
      </Block>

      <Block name="buttons">
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
      </Block>

      <Block name="rule">
        <Rule />
      </Block>
    </main>
  );
}
