/**
 * How numbers, rates and timestamps are written. One module because the report and
 * the public board show the same quantities, and a rate printed to one decimal on
 * one page and two on the other is the drift that never gets noticed.
 *
 * Nothing here recomputes a statistic. Both Wilson bounds and the point estimate
 * arrive from the API, which derives them in `app/stats.py` — a second
 * implementation in TypeScript would be free to disagree with the ranking.
 */

/** A rate as a percentage to one decimal. */
export function formatPercent(rate: number | null): string {
  if (rate === null) return "—";
  return `${(rate * 100).toFixed(1)}%`;
}

/**
 * An interval as a bare range in points, because the column header carries the unit
 * and a `%` on both ends reads as two numbers rather than one span.
 */
export function formatPoints(lower: number | null, upper: number | null): string {
  if (lower === null || upper === null) return "—";
  return `${(lower * 100).toFixed(1)}–${(upper * 100).toFixed(1)}`;
}

/** UTC day only, for a column whose header says nothing finer is meant. */
export function formatDay(iso: string | null): string {
  return iso ? iso.slice(0, 10) : "—";
}

/** Day and minute with the zone spelled out, for prose and label/value rows. */
export function formatUtc(iso: string | null): string {
  if (!iso) return "—";
  return `${iso.slice(0, 10)} ${iso.slice(11, 16)} UTC`;
}

/** Table-column form: the unit moves to the header so the cell stays one line. */
export function formatUtcCompact(iso: string | null): string {
  if (!iso) return "—";
  return `${iso.slice(0, 10)} ${iso.slice(11, 16)}`;
}

export function formatDuration(seconds: number | null): string {
  if (seconds === null) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${Math.round(seconds % 60)}s`;
}
