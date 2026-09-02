import clsx from "clsx";
import type { ReactNode } from "react";

/** Uppercase mono section label — 11px, wide tracking, faint. */
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

/** Full-column hairline. Separators are rules, never boxes. */
export function Rule() {
  return <hr className="border-0 border-t border-border" />;
}

export function Button({
  children,
  variant = "primary",
  disabled = false,
  type = "button",
  size = "default",
}: {
  children: ReactNode;
  variant?: "primary" | "secondary";
  disabled?: boolean;
  /** `submit` is what makes this usable inside a server-action form. */
  type?: "button" | "submit";
  /** `compact` fits the 56px nav bar; everything else uses the default. */
  size?: "default" | "compact";
}) {
  return (
    <button
      type={type}
      disabled={disabled}
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
