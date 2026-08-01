import type { SendState } from "../api/types";

/**
 * `uncertain` renders **amber, not red**.
 *
 * Failed means we know nothing was sent. Uncertain means we do not know.
 * Conflating them invites exactly the wrong action — retrying a message that
 * already went out (design D5).
 */
const LABELS: Record<SendState, { text: string; className: string }> = {
  in_flight: { text: "in flight", className: "badge badge-flight" },
  delivered: { text: "delivered", className: "badge badge-ok" },
  failed_transient: { text: "failed (transient)", className: "badge badge-bad" },
  failed_permanent: { text: "failed", className: "badge badge-bad" },
  uncertain: { text: "uncertain", className: "badge badge-doubt" },
};

export function StateBadge({ state }: { state: SendState }) {
  const view = LABELS[state] ?? { text: state, className: "badge" };
  return <span className={view.className}>{view.text}</span>;
}
