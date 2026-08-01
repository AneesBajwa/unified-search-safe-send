/** Presentation helpers. Nothing here decides anything. */

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 60) return `${Math.max(seconds, 0)}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

/** For the places where "when exactly" is the question — send detail, mostly. */
export function absoluteTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return "—";
  return then.toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "medium",
  });
}

/**
 * A label, never a branch.
 *
 * `source` is free text from the API — the closed `Result` shape says so — and
 * the console renders whatever arrives. A `switch` here would be the first
 * place a fourth source silently stopped working.
 */
export function sourceLabel(source: string): string {
  if (source.startsWith("fault:")) return source;
  return source.charAt(0).toUpperCase() + source.slice(1);
}

/** Whole seconds from now until `iso`, floored at zero. */
export function secondsUntil(iso: string | null | undefined, now: number): number {
  if (!iso) return 0;
  const target = new Date(iso).getTime();
  if (Number.isNaN(target)) return 0;
  return Math.max(0, Math.ceil((target - now) / 1000));
}

/** "you marked this delivered" reads very differently from "marked_delivered". */
export function resolutionLabel(resolution: string): string {
  if (resolution === "marked_delivered") return "You marked this delivered";
  if (resolution === "forced_resend") return "You asked for it to be sent again";
  return resolution;
}
