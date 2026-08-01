import { Link } from "react-router-dom";
import { useSearchHistory, useSends } from "../api/hooks";
import { EmptyState, SeedBadge } from "../components/EmptyState";
import { RetryCountdown } from "../components/RetryCountdown";
import { StateBadge } from "../components/StateBadge";
import { relativeTime } from "../lib/format";

export function HistoryPage() {
  const sends = useSends();
  const searches = useSearchHistory();

  return (
    <section>
      <h2>Sends</h2>
      {(sends.data ?? []).length === 0 ? (
        <EmptyState>Nothing sent yet.</EmptyState>
      ) : (
        <ul className="rows">
          {(sends.data ?? []).map((send) => (
            <li key={send.send_id}>
              <Link to={`/history/sends/${send.send_id}`}>
                <StateBadge state={send.state} />
                <span className="row-title">
                  {send.recipient_display} · {send.subject ?? send.body?.slice(0, 48)}
                </span>
                <RetryCountdown send={send} />
                <SeedBadge isSeed={send.is_seed} />
                <span className="muted">{relativeTime(send.created_at)}</span>
              </Link>
            </li>
          ))}
        </ul>
      )}

      <h2>Searches</h2>
      {(searches.data ?? []).length === 0 ? (
        <EmptyState>No searches yet.</EmptyState>
      ) : (
        <ul className="rows">
          {(searches.data ?? []).map((search) => (
            <li key={search.search_id}>
              <span className="row-title">{search.query}</span>
              <span className="muted">
                {search.result_count} results ·{" "}
                {search.finished ? "finished" : "still running"}
              </span>
              <SeedBadge isSeed={search.is_seed} />
              <span className="muted">{relativeTime(search.created_at)}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
