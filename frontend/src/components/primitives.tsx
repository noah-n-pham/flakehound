import clsx from "clsx";
import Link from "next/link";
import type { ReactNode } from "react";

/** Uppercase mono section label: 11px, wide tracking, faint. */
export function SectionLabel({ children }: { children: ReactNode }) {
  return (
    <p className="font-mono text-[11px] uppercase tracking-[0.12em] text-text-faint">
      {children}
    </p>
  );
}

/**
 * Two-tone heading: first clause primary, second muted, inside one sentence.
 * The strongest identity marker in DESIGN.md, so it is a component rather than
 * a pair of spans copied per page.
 */
export function TwoToneHeading({
  lead,
  trail,
  className,
}: {
  lead: string;
  trail: string;
  className?: string;
}) {
  return (
    <h1
      className={clsx(
        "font-display text-[44px] leading-[48px] font-light",
        className,
      )}
    >
      <span className="text-text">{lead}</span>{" "}
      <span className="text-text-muted">{trail}</span>
    </h1>
  );
}

/**
 * h2 and h3 from DESIGN.md's scale. The two-tone h1 is a separate component rather
 * than `level={1}`, because what makes it the identity marker is its two clauses,
 * not its size.
 */
export function Heading({
  level,
  children,
  className,
}: {
  level: 2 | 3;
  children: ReactNode;
  className?: string;
}) {
  const Tag = level === 2 ? "h2" : "h3";
  return (
    <Tag
      className={clsx(
        "font-display font-light text-text",
        level === 2 ? "text-[28px]" : "text-[20px]",
        className,
      )}
    >
      {children}
    </Tag>
  );
}

/**
 * Body copy: 15px Inter at line-height 1.6, muted. A component and not three
 * utility classes, because those three were being retyped on every paragraph and
 * the day one of them says `leading-[1.5]` nobody will notice.
 */
export function Body({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <p className={clsx("text-[15px] leading-[1.6] text-text-muted", className)}>
      {children}
    </p>
  );
}

/**
 * The content column. 680px, or 960px when a data table needs the room. Those are
 * the only two widths DESIGN.md allows, which is why this takes a boolean and not a
 * width.
 */
export function Page({
  wide = false,
  children,
}: {
  wide?: boolean;
  children: ReactNode;
}) {
  return (
    <main
      className={clsx(
        "mx-auto px-6 py-24",
        wide ? "max-w-[960px]" : "max-w-[680px]",
      )}
    >
      {children}
    </main>
  );
}

/**
 * The dot-separated mono line that sits under a statistic: facts about how a number
 * was produced, small and faint enough to ignore.
 */
export function MetaStrip({ items }: { items: ReactNode[] }) {
  return (
    <p className="font-mono text-[11px] leading-[1.8] text-text-faint">
      {items.map((item, index) => (
        <span key={index}>
          {index > 0 ? <span className="px-2">·</span> : null}
          {item}
        </span>
      ))}
    </p>
  );
}

/**
 * A major page section: 96px of rhythm above it, an uppercase mono label, 32px
 * between label and content. Every page was hand-rolling that triple and
 * `/styleguide` kept a private copy of it: drift in the file whose job is to
 * prevent drift.
 */
export function Section({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <section className="mt-24">
      <SectionLabel>{label}</SectionLabel>
      <div className="mt-8">{children}</div>
    </section>
  );
}

/** Very large light number, small unit beside it, mono caption underneath. */
export function StatBlock({
  value,
  unit,
  label,
  caption,
}: {
  value: string;
  unit?: string;
  label: string;
  caption?: string;
}) {
  return (
    <div>
      <p className="font-display text-[64px] leading-[64px] font-light text-text">
        {value}
        {unit ? (
          <span className="text-[26px] text-text-muted"> {unit}</span>
        ) : null}
      </p>
      <p className="mt-4 text-[13px] text-text-muted">{label}</p>
      {caption ? (
        <p className="mt-1 font-mono text-[11px] text-text-faint">{caption}</p>
      ) : null}
    </div>
  );
}

/** Sans label left, mono value right, hairline rule beneath. */
export function LabelValueRow({
  label,
  value,
  tone = "default",
}: {
  label: string;
  value: ReactNode;
  tone?: Tone;
}) {
  return (
    <div className="flex items-baseline justify-between gap-8 border-b border-border py-3">
      <span className="text-[15px] text-text-muted">{label}</span>
      <span className={clsx("font-mono text-[13px]", toneClass(tone))}>
        {value}
      </span>
    </div>
  );
}

export type Tone = "default" | "muted" | "ok" | "warn" | "bad";

export function toneClass(tone: Tone): string {
  switch (tone) {
    case "ok":
      return "text-ok";
    case "warn":
      return "text-warn";
    case "bad":
      return "text-bad";
    case "muted":
      return "text-text-muted";
    default:
      return "text-text";
  }
}

/**
 * The one bar geometry in the app: an 88px hairline track, a faint span between two
 * values, and an optional bright tick. `IntervalBar` and `ShareBar` are two meanings
 * of it, not two implementations. The second version of this was the thing worth
 * avoiding, so the semantics live in the wrappers and the arithmetic lives here.
 *
 * `max` is the top of the scale and belongs to the *page*, never the row: every bar in
 * one table has to share a scale or the widths compare nothing.
 */
function Track({
  from,
  to,
  tick,
  max,
}: {
  from: number;
  to: number;
  tick?: number | null;
  max: number;
}) {
  const scale = max > 0 ? max : 1;
  const position = (value: number) => Math.min(Math.max(value / scale, 0), 1) * 100;
  const left = position(from);
  // A zero-width span is a real answer (a very certain interval nearly is one, and a
  // job that burned almost no time is one), so it has to stay visible.
  const width = Math.max(position(to) - left, 0.8);

  return (
    <span className="relative inline-block h-[7px] w-[88px] border-b border-border align-middle">
      <span
        className="absolute top-[2px] h-[3px] bg-text-faint"
        style={{ left: `${left}%`, width: `${width}%` }}
      />
      {tick === null || tick === undefined ? null : (
        <span
          className="absolute top-0 h-[7px] w-px bg-text"
          style={{ left: `${position(tick)}%` }}
        />
      )}
    </span>
  );
}

/**
 * A confidence interval drawn to scale: the interval as a faint span, the point
 * estimate as a bright tick inside it. Beside the printed range it answers the
 * question the numbers make you do arithmetic for: how wide is this, next to the row
 * above it. The printed range carries the absolute values, so the bar never has to be
 * read off an axis it does not have.
 */
export function IntervalBar({
  lower,
  upper,
  point,
  max = 1,
}: {
  lower: number;
  upper: number;
  point?: number | null;
  /** Rate at the right-hand end of the track, as a fraction. */
  max?: number;
}) {
  return <Track from={lower} to={upper} tick={point} max={max} />;
}

/**
 * One magnitude from zero, for a share of a total. No tick, because unlike an interval
 * a share has one value and nothing to estimate. The bar *is* the number.
 *
 * `max` is the largest share on the page rather than 1, so the rows of a table whose
 * biggest slice is 12% are still comparable with each other instead of all hugging the
 * left edge. The printed percentage is what says which it is.
 */
export function ShareBar({ value, max = 1 }: { value: number; max?: number }) {
  return <Track from={0} to={value} max={max} />;
}

export type TrendSeries = {
  name: string;
  /**
   * One entry per step of the x axis, `null` where there is no observation. The
   * caller aligns the array to the calendar. This component spaces entries evenly,
   * so a series that omitted its empty days would silently compress time.
   */
  values: (number | null)[];
  /** The line worth reading first. Exactly one series should set it. */
  emphasis?: boolean;
  /** What the last known value is, already formatted. */
  lastLabel: string;
};

/** Runs of consecutive observations, so a gap is drawn as a gap and not bridged. */
function segments(values: (number | null)[]): { index: number; value: number }[][] {
  const runs: { index: number; value: number }[][] = [];
  values.forEach((value, index) => {
    if (value === null) {
      if (runs.at(-1)?.length) runs.push([]);
      return;
    }
    if (runs.length === 0) runs.push([]);
    runs.at(-1)!.push({ index, value });
  });
  return runs.filter((run) => run.length > 0);
}

/**
 * A line chart with no chrome: no gridlines, no axis ticks, no legend box. The scale
 * is stated once as a peak label and the ends of the x axis are named; everything
 * between is the shape of the line, which is the only thing a trend is for.
 *
 * It is an inline SVG in a server component rather than a charting library. recharts
 * is on the allowlist and would work, but it renders on the client, and this app has
 * exactly one client component on purpose. A chart of thirty numbers that never
 * changes after render does not need JavaScript in the browser to draw it.
 *
 * `preserveAspectRatio="none"` plus `vectorEffect="non-scaling-stroke"` is what lets
 * the chart fill the column at any width while the hairline stays exactly 1px.
 */
export function TrendChart({
  series,
  max,
  peakLabel,
  xLabels,
}: {
  series: TrendSeries[];
  /** Top of the y scale. Zero is always the bottom: durations start there. */
  max: number;
  /** The scale, stated in words because the chart has no axis. */
  peakLabel: string;
  xLabels: [string, string];
}) {
  const steps = Math.max(...series.map((line) => line.values.length), 2);
  const scale = max > 0 ? max : 1;
  const x = (index: number) => (steps === 1 ? 0 : (index / (steps - 1)) * 100);
  const y = (value: number) => 40 - Math.min(Math.max(value / scale, 0), 1) * 40;

  return (
    <div>
      <p className="font-mono text-[11px] text-text-faint">{peakLabel}</p>
      <svg
        viewBox="0 0 100 40"
        preserveAspectRatio="none"
        className="mt-2 h-[120px] w-full border-b border-border"
        role="img"
        aria-label={`${series.map((line) => line.name).join(" and ")} trend`}
      >
        {series.map((line) =>
          segments(line.values).map((run, index) => {
            const stroke = line.emphasis ? "stroke-text" : "stroke-text-faint";
            // A lone observation between two gaps is still a fact, and a polyline of
            // one point draws nothing at all, so it becomes a dot instead.
            if (run.length === 1) {
              return (
                <circle
                  key={`${line.name}-${index}`}
                  cx={x(run[0].index)}
                  cy={y(run[0].value)}
                  r={0.8}
                  className={clsx(stroke, line.emphasis ? "fill-text" : "fill-text-faint")}
                />
              );
            }
            return (
              <polyline
                key={`${line.name}-${index}`}
                points={run.map((p) => `${x(p.index)},${y(p.value)}`).join(" ")}
                fill="none"
                strokeWidth={1}
                vectorEffect="non-scaling-stroke"
                className={stroke}
              />
            );
          }),
        )}
      </svg>
      <div className="mt-2 flex justify-between font-mono text-[11px] text-text-faint">
        <span>{xLabels[0]}</span>
        <span>{xLabels[1]}</span>
      </div>
      <div className="mt-4 flex flex-wrap items-center gap-x-6 gap-y-2 font-mono text-[11px] text-text-faint">
        {series.map((line) => (
          <span key={line.name} className="inline-flex items-center gap-2">
            <span
              className={clsx(
                "inline-block h-px w-4 align-middle",
                line.emphasis ? "bg-text" : "bg-text-faint",
              )}
            />
            {line.name} · {line.lastLabel}
          </span>
        ))}
      </div>
    </div>
  );
}

export type TimelineMark = { outcome: string | null };

export type TimelineCommit = {
  sha: string;
  /** What the commit's attempts add up to, decided by the API, not here. */
  state: "flaked" | "failed" | "passed" | "unjudged";
  /** One mark per attempt, in order. A commit is a bracket around its attempts. */
  marks: TimelineMark[];
  /** Hover text: the sha and its counts, since the strip itself has no room. */
  title: string;
};

const MARK = "inline-block h-5 w-[9px] align-middle";

function markClass(outcome: string | null): string {
  // A pass is not news, so it is not coloured. Only a failure and an unjudged run
  // get anything, which keeps a 30-commit strip monochrome until something is wrong.
  if (outcome === "failure") return `${MARK} bg-bad`;
  if (outcome === "success") return `${MARK} bg-text-faint`;
  return `${MARK} border border-border`;
}

/**
 * One job's pass/fail history by commit, oldest at the left.
 *
 * Each mark is one attempt and each group is one commit, underlined so the group
 * reads as a bracket: a failure beside a pass under one rule is a flake, which is
 * the whole shape both signals look for. The commit's own verdict comes from the
 * API (`flaked` beats `failed` beats `passed`) and is drawn on that rule, so the
 * timeline never re-decides what the leaderboard already ranked.
 *
 * Attempts rather than commits are the marks because a re-run recovery lives
 * *inside* one commit. Collapsing it to a single mark would hide the evidence for
 * the signal that found it.
 */
export function Timeline({ commits }: { commits: TimelineCommit[] }) {
  return (
    <div>
      <div className="overflow-x-auto pb-2">
        <div className="flex items-end gap-[10px]">
          {commits.map((commit) => (
            <span
              key={commit.sha}
              title={commit.title}
              className={clsx(
                "inline-flex gap-[2px] border-b pb-1",
                commit.state === "flaked" ? "border-warn" : "border-border",
              )}
            >
              {commit.marks.map((mark, index) => (
                <span key={index} className={markClass(mark.outcome)} />
              ))}
            </span>
          ))}
        </div>
      </div>
      <div className="mt-4 flex flex-wrap items-center gap-x-6 gap-y-2 font-mono text-[11px] text-text-faint">
        <span className="inline-flex items-center gap-2">
          <span className={markClass("success")} />
          passed
        </span>
        <span className="inline-flex items-center gap-2">
          <span className={markClass("failure")} />
          failed
        </span>
        <span className="inline-flex items-center gap-2">
          <span className={markClass(null)} />
          not judgeable
        </span>
        <span className="inline-flex items-center gap-2">
          <span className="inline-block h-5 w-[9px] border-b border-warn align-middle" />
          commit flaked
        </span>
        <span>one mark per attempt · one group per commit · oldest first</span>
      </div>
    </div>
  );
}

/**
 * Mono links choosing which one thing a page is about. The current choice is plain
 * text rather than a link to itself, which is also the only marker. The nav has no
 * active state either, and inventing a highlight here would be a second convention.
 */
export function Switcher({
  items,
}: {
  items: { label: string; href: string; current: boolean }[];
}) {
  return (
    <div className="flex flex-wrap items-center gap-x-6 gap-y-2 font-mono text-[13px]">
      {items.map((item) =>
        item.current ? (
          <span key={item.href} className="text-text">
            {item.label}
          </span>
        ) : (
          <Link
            key={item.href}
            href={item.href}
            className="text-text-muted transition-colors duration-150 hover:text-text"
          >
            {item.label}
          </Link>
        ),
      )}
    </div>
  );
}

/** Full-column hairline. Separators are rules, never boxes. */
export function Rule() {
  return <hr className="border-0 border-t border-border" />;
}

/**
 * A link inside body copy. Monochrome, so the only affordance available is the
 * underline, which is why it is a hairline in the border token rather than the
 * text colour, and why hovering lifts the text instead of changing its hue.
 */
export function InlineLink({
  href,
  children,
}: {
  href: string;
  children: ReactNode;
}) {
  return (
    <Link
      href={href}
      className="text-text underline decoration-border-strong decoration-1 underline-offset-4 transition-colors duration-150 hover:decoration-text-muted"
    >
      {children}
    </Link>
  );
}

/**
 * A link off this site, for a table cell rather than a sentence.
 *
 * Separate from `InlineLink` for two reasons that both come from the public board.
 * It carries `target`/`rel`, because sending a reader to github.com to check a claim
 * should not cost them the page they were reading. And it is available in mono, since
 * every one of these is an identifier (a repo name, a run id) and DESIGN.md puts
 * data in the mono face regardless of it being a link.
 */
export function ExternalLink({
  href,
  mono = false,
  children,
}: {
  href: string;
  mono?: boolean;
  children: ReactNode;
}) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer noopener"
      className={clsx(
        "underline decoration-border-strong decoration-1 underline-offset-4 transition-colors duration-150 hover:decoration-text-muted",
        mono ? "font-mono text-text" : "text-text",
      )}
    >
      {children}
    </a>
  );
}

export function Button({
  children,
  variant = "primary",
  disabled = false,
  type = "button",
  size = "default",
  onClick,
}: {
  children: ReactNode;
  variant?: "primary" | "secondary";
  disabled?: boolean;
  /** `submit` is what makes this usable inside a server-action form. */
  type?: "button" | "submit";
  /** `compact` fits the 56px nav bar; everything else uses the default. */
  size?: "default" | "compact";
  /**
   * Only passable from a client component, which in this app means the error
   * boundary's retry and nothing else. Every other action is a server action in a
   * form. `/styleguide` therefore shows this button without a handler.
   */
  onClick?: () => void;
}) {
  return (
    <button
      type={type}
      disabled={disabled}
      onClick={onClick}
      className={clsx(
        "rounded-[2px] transition-opacity duration-150",
        size === "compact" ? "px-3 py-1.5 text-[12px]" : "px-4 py-2 text-[13px]",
        disabled
          ? "cursor-not-allowed border border-border text-text-faint"
          : variant === "primary"
            ? "bg-btn-fill text-btn-fill-text hover:opacity-80"
            : "border border-border text-text-muted hover:border-border-strong",
      )}
    >
      {children}
    </button>
  );
}

export type Column = { key: string; header: string; numeric?: boolean };

/** Hairline rules only. No zebra striping, no vertical borders. */
export function Table({
  columns,
  rows,
}: {
  columns: Column[];
  rows: Record<string, ReactNode>[];
}) {
  // Below the 680px content column a data table has to give somewhere; scrolling
  // it sideways keeps every row on one line, which wrapping would not.
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse">
        <thead>
          <tr className="bg-bg-elevated">
            {columns.map((column) => (
              <th
                key={column.key}
                className={clsx(
                  "whitespace-nowrap border-b border-border px-3 py-2 font-mono text-[11px] font-normal uppercase tracking-[0.12em] text-text-faint",
                  column.numeric ? "text-right" : "text-left",
                )}
              >
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>
              {columns.map((column) => (
                <td
                  key={column.key}
                  className={clsx(
                    "whitespace-nowrap border-b border-border px-3 py-3 text-[13px]",
                    column.numeric
                      ? "text-right font-mono text-text"
                      : "text-text-muted",
                  )}
                >
                  {row[column.key]}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
