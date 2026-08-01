import { useConnections } from "../api/hooks";
import { EmptyState } from "../components/EmptyState";

/**
 * ⏸️ Placeholder. Real OAuth, reconnect and disconnect are group 6.
 *
 * It exists now because the connection a send goes out from is part of what the
 * confirm screen shows, and because a page that says plainly "these are fakes"
 * is better than one that implies a grant exists.
 */
export function ConnectionsPage() {
  const connections = useConnections();

  return (
    <section>
      <h2>Connections</h2>
      <p className="muted">
        These are fake connections provisioned at sign-in so the product loop runs
        with no OAuth at all. Connect, reconnect and disconnect land with group 6.
      </p>
      {(connections.data ?? []).length === 0 ? (
        <EmptyState>No connections.</EmptyState>
      ) : (
        <ul className="rows">
          {(connections.data ?? []).map((connection) => (
            <li key={connection.id}>
              <span className="row-title">{connection.display_name}</span>
              <span className="muted">
                {connection.provider} · {connection.status}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
