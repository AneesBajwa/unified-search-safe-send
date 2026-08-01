import type { SourceView } from "../api/types";
import { sourceLabel } from "../lib/format";

/**
 * Five states, and the distinction that matters most is between two of them:
 *
 *   done, 0 results → "we looked and found nothing"
 *   failed         → "we did not look"
 *
 * Rendering those the same way lets a customer conclude "nobody emailed me
 * about this" when the truth is that we could not reach their mailbox. That is
 * the single most dangerous thing this UI could do (risks.md R16), which is why
 * a source that is merely mocked also says so out loud.
 */
export function SourceStatusChip({ source }: { source: SourceView }) {
  const label = sourceLabel(source.source);
  const mode = source.mode === "live" ? "" : ` · ${source.mode}`;

  if (source.status === "pending" || source.status === "running") {
    return (
      <span className="chip chip-running">
        {label} · searching…{mode}
      </span>
    );
  }

  if (source.status === "needs_reconnect") {
    return (
      <a className="chip chip-reconnect" href={source.error?.reconnect_url ?? "/connections"}>
        {label} · reconnect
      </a>
    );
  }

  if (source.status === "failed") {
    return (
      <span className="chip chip-failed" title={source.error?.message ?? ""}>
        {label} · unavailable{mode}
      </span>
    );
  }

  return (
    <span className="chip chip-done">
      {label} ·{" "}
      {source.result_count > 0 ? source.result_count : "no matches"}
      {mode}
    </span>
  );
}
