import { ERROR_CODES } from "../api/errors";
import type { SourceView } from "../api/types";
import { sourceLabel } from "../lib/format";

/**
 * The single most dangerous component in the app, and the reason is arithmetic:
 * a source that failed silently makes an incomplete answer look complete, and
 * the customer concludes "nobody emailed me about this" when the truth is that
 * we never looked (`risks.md` R16).
 *
 * 🔴 **There are three ways to have no results, not two.**
 *
 * | status | `error.code` | means | reads |
 * |---|---|---|---|
 * | `done`, 0 | — | we looked and found nothing | "no matches" |
 * | `failed`, transient | `provider_unavailable` | we could not look, **yet** | "retrying" |
 * | `failed`, permanent | `provider_unavailable` | we could not look, something is wrong | "unavailable" |
 * | `needs_reconnect` | `connection_needs_reconnect` | one click fixes it | **Reconnect** |
 * | `needs_reconnect` | `connection_not_connected` | never connected | **Connect** |
 *
 * **Branch on `error.code`, not on `status`.** Both actionable rows carry
 * `status: "needs_reconnect"`; only the revoked one also carries
 * `reconnect_url`. The verb is the entire difference — offering "Reconnect
 * Gmail" for an account somebody never linked reads as though we lost something
 * of theirs, and the never-connected row is the state **every new user sees
 * first**.
 *
 * The action is a button, never a link. `action_url` is an API route that needs
 * the `X-API-Key` header and answers `{authorize_url}`; rendered as an `<a
 * href>` it resolves against the SPA's own origin and goes nowhere. Following
 * it rather than reading it is the whole lesson of the last two phases.
 *
 * Reading `error.classification` to choose amber over red is *rendering* the
 * API's decision, not making one. Nothing here computes whether an error is
 * retryable.
 */

export interface SourceStatusChipProps {
  source: SourceView;
  /** `false` while any source may still change — a failure may yet be retried. */
  searchFinished: boolean;
  /**
   * True when this provider has more than one grant in the same search, so the
   * chip has to name the account. `(source, connection_id)` is the key, not
   * `source` — two Gmail accounts are two independent runs that succeed or fail
   * separately, and "Gmail · reconnect" beside "Gmail · 4" is unreadable
   * without saying *which* Gmail.
   */
  disambiguate?: boolean;
  /** Handed the payload's `action_url`, verbatim. */
  onAction?: (actionUrl: string) => void;
}

export function SourceStatusChip({
  source,
  searchFinished,
  disambiguate,
  onAction,
}: SourceStatusChipProps) {
  const label =
    disambiguate && source.display_name
      ? `${sourceLabel(source.source)} · ${source.display_name}`
      : sourceLabel(source.source);
  // Always visible, never silent: a mocked source that reads as live is the
  // dishonesty this component exists to prevent, aimed at ourselves.
  const mode =
    source.mode === "live" ? null : <span className="chip-mode">{source.mode}</span>;
  const code = source.error?.code;
  const actionUrl = source.error?.action_url ?? undefined;

  if (code === ERROR_CODES.needsReconnect || code === ERROR_CODES.notConnected) {
    const verb = code === ERROR_CODES.needsReconnect ? "Reconnect" : "Connect";
    return (
      <button
        type="button"
        className="chip chip-action"
        data-state={code}
        disabled={!actionUrl || !onAction}
        onClick={() => {
          if (actionUrl && onAction) onAction(actionUrl);
        }}
      >
        <span className="chip-source">{label}</span>
        <span className="chip-verb">{verb}</span>
        {mode}
      </button>
    );
  }

  if (source.status === "pending" || source.status === "running") {
    return (
      <span className="chip chip-running" data-state="running">
        <span className="chip-source">{label}</span>
        <span className="chip-value">
          searching<span className="ellipsis" aria-hidden="true" />
        </span>
        {mode}
      </span>
    );
  }

  if (source.status === "failed") {
    // Transient means the answer may still arrive; permanent means it will not.
    // Rendering them the same way is the difference between "wait" and "this is
    // as good as it gets", and the customer acts on whichever they believe.
    const retrying = source.error?.classification === "transient" && !searchFinished;
    return (
      <span
        className={retrying ? "chip chip-retrying" : "chip chip-failed"}
        data-state={retrying ? "retrying" : "failed"}
      >
        <span className="chip-source">{label}</span>
        <span className="chip-value">{retrying ? "retrying" : "unavailable"}</span>
        {mode}
      </span>
    );
  }

  const found = source.result_count > 0;
  return (
    <span
      className={found ? "chip chip-done" : "chip chip-empty"}
      data-state={found ? "done" : "empty"}
    >
      <span className="chip-source">{label}</span>
      <span className="chip-value">{found ? source.result_count : "no matches"}</span>
      {mode}
    </span>
  );
}
