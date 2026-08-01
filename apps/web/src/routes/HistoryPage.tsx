import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useRerunSearch, useSearchHistory, useSends } from "../api/hooks";
import { EmptyState, SeedBadge } from "../components/EmptyState";
import { RetryCountdown } from "../components/RetryCountdown";
import { StateBadge } from "../components/StateBadge";
import { relativeTime } from "../lib/format";

type Tab = "sends" | "searches";

/**
 * History. Trust rank 4 — it matters once something has already gone wrong,
 * which is exactly why the list stays a list and the *detail* carries the
 * evidence. History list polish is on the cut list; detail never is.
 *
 * `include_seed=false` is what makes "show me only what I actually did"
 * answerable. Seed rows exist so the product demos from a cold start; a listing
 * with no way to exclude them turns that into a cost.
 */
export function HistoryPage() {
  const [tab, setTab] = useState<Tab>("sends");
  const [includeSeed, setIncludeSeed] = useState(true);

  return (
    <section className="page">
      <header className="page-head">
        <h2>History</h2>
        <p className="page-sub">
          Every send and every search, including the ones that failed — especially those.
        </p>
      </header>

      <div className="toolbar">
        <div className="segmented" role="tablist" aria-label="History type">
          <button
            type="button"
            role="tab"
            aria-selected={tab === "sends"}
            className={tab === "sends" ? "segment segment-on" : "segment"}
            onClick={() => setTab("sends")}
          >
            Sends
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === "searches"}
            className={tab === "searches" ? "segment segment-on" : "segment"}
            onClick={() => setTab("searches")}
          >
            Searches
          </button>
        </div>
        <label className="switch">
          <input
            type="checkbox"
            checked={!includeSeed}
            onChange={(event) => setIncludeSeed(!event.target.checked)}
          />
          <span>Only what I did</span>
        </label>
      </div>

      {tab === "sends" ? (
        <SendList includeSeed={includeSeed} />
      ) : (
        <SearchList includeSeed={includeSeed} />
      )}
    </section>
  );
}

function SendList({ includeSeed }: { includeSeed: boolean }) {
  const sends = useSends(includeSeed);
  const rows = sends.data ?? [];

  if (sends.isLoading) return <p className="muted">Loading…</p>;
  if (rows.length === 0) {
    return (
      <EmptyState title="Nothing sent yet">
        Compose a message and the gate will record it here — delivered or not.
      </EmptyState>
    );
  }

  return (
    <>
      <ul className="rows rows-cards">
        {rows.map((send) => (
          <li key={send.send_id} className="card row-card">
            <Link className="row-link" to={`/history/sends/${send.send_id}`}>
              <span className="row-top">
                <StateBadge state={send.state} />
                <SeedBadge isSeed={send.is_seed} />
                <span className="row-time">{relativeTime(send.created_at)}</span>
              </span>
              <span className="row-title">{send.recipient_display}</span>
              <span className="row-sub">{send.subject || send.body || ""}</span>
              <span className="row-meta">
                <RetryCountdown send={send} />
                {send.state !== "in_flight" && send.attempts > 1 ? (
                  <span className="countdown">
                    {send.attempts} attempts
                  </span>
                ) : null}
              </span>
            </Link>
          </li>
        ))}
      </ul>
      <LoadMore
        hasMore={Boolean(sends.hasNextPage)}
        loading={sends.isFetchingNextPage}
        onClick={() => void sends.fetchNextPage()}
      />
    </>
  );
}

function SearchList({ includeSeed }: { includeSeed: boolean }) {
  const searches = useSearchHistory(includeSeed);
  const rerun = useRerunSearch();
  const navigate = useNavigate();
  const rows = searches.data ?? [];

  if (searches.isLoading) return <p className="muted">Loading…</p>;
  if (rows.length === 0) {
    return <EmptyState title="No searches yet">Run one and it lands here.</EmptyState>;
  }

  return (
    <>
      <ul className="rows rows-cards">
        {rows.map((search) => (
          <li key={search.search_id} className="card row-card">
            <Link className="row-link" to={`/search/${search.search_id}`}>
              <span className="row-top">
                <SeedBadge isSeed={search.is_seed} />
                <span className="row-time">{relativeTime(search.created_at)}</span>
              </span>
              <span className="row-title">{search.query}</span>
              <span className="row-sub">
                {search.result_count} result{search.result_count === 1 ? "" : "s"} ·{" "}
                {search.finished ? "finished" : "still running"}
              </span>
            </Link>
            <div className="row-actions">
              <button
                type="button"
                className="button button-quiet"
                disabled={rerun.isPending}
                onClick={() =>
                  rerun.mutate(search.search_id, {
                    onSuccess: (created) => navigate(`/search/${created.search_id}`),
                  })
                }
              >
                Run it again
              </button>
            </div>
          </li>
        ))}
      </ul>
      <LoadMore
        hasMore={Boolean(searches.hasNextPage)}
        loading={searches.isFetchingNextPage}
        onClick={() => void searches.fetchNextPage()}
      />
    </>
  );
}

function LoadMore({
  hasMore,
  loading,
  onClick,
}: {
  hasMore: boolean;
  loading: boolean;
  onClick: () => void;
}) {
  if (!hasMore) return null;
  return (
    <div className="load-more">
      <button type="button" className="button" disabled={loading} onClick={onClick}>
        {loading ? "Loading…" : "Load more"}
      </button>
    </div>
  );
}
