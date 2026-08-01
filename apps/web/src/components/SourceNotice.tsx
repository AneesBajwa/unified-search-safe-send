import { ERROR_CODES } from "../api/errors";
import type { SourceView } from "../api/types";
import { sourceLabel } from "../lib/format";

/**
 * The reason, in full, under the chips (task 11.5).
 *
 * The chip is the glance; this is the sentence. On a phone there is no hover,
 * so a reason parked in a `title` attribute is a reason nobody will ever read —
 * and "we could not look" has to be legible or it may as well not have been
 * reported. The provider's own words are shown untruncated: a paraphrase is how
 * an operator ends up guessing.
 */
export function SourceNotice({
  source,
  onAction,
}: {
  source: SourceView;
  onAction?: (actionUrl: string) => void;
}) {
  const error = source.error;
  if (!error) return null;

  const code = error.code;
  const actionable =
    code === ERROR_CODES.needsReconnect || code === ERROR_CODES.notConnected;
  const actionUrl = error.action_url ?? undefined;
  // Named by account when there is one: with two grants on a provider, "Gmail
  // needs reconnecting" does not say which of them.
  const label = source.display_name
    ? `${sourceLabel(source.source)} · ${source.display_name}`
    : sourceLabel(source.source);

  const headline = actionable
    ? code === ERROR_CODES.needsReconnect
      ? `${label} needs reconnecting`
      : `${label} is not connected yet`
    : `${label} could not be searched`;

  return (
    <div className={actionable ? "notice notice-action" : "notice notice-bad"}>
      <div className="notice-body">
        <p className="notice-title">{headline}</p>
        <p className="notice-detail">{error.message}</p>
        {actionable ? null : (
          <p className="notice-meta">
            No results from this source — which is not the same as no results.
          </p>
        )}
      </div>
      {actionable && actionUrl && onAction ? (
        <button
          type="button"
          className="button button-primary notice-cta"
          onClick={() => onAction(actionUrl)}
        >
          {code === ERROR_CODES.needsReconnect ? "Reconnect" : "Connect"} {label}
        </button>
      ) : null}
    </div>
  );
}
