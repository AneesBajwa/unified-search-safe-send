/**
 * `uncertain` renders **amber, not red**.
 *
 * Failed means we know nothing was sent. Uncertain means we do not know.
 * Conflating them invites exactly the wrong action — retrying a message that
 * already went out (design D5).
 *
 * The map is keyed by the API's own state strings and falls through to the raw
 * value, so a state this console has never heard of renders as itself rather
 * than as nothing at all.
 */
const LABELS: Record<string, { text: string; className: string }> = {
  in_flight: { text: "in flight", className: "badge badge-flight" },
  delivered: { text: "delivered", className: "badge badge-ok" },
  failed_transient: { text: "failed", className: "badge badge-bad" },
  failed_permanent: { text: "failed", className: "badge badge-bad" },
  uncertain: { text: "uncertain", className: "badge badge-doubt" },
};

export function StateBadge({ state }: { state: string }) {
  const view = LABELS[state] ?? { text: state.replace(/_/g, " "), className: "badge" };
  return (
    <span className={view.className} data-state={state}>
      {view.text}
    </span>
  );
}
