import { useState } from "react";
import { useCreateSearch, useSearch } from "../api/hooks";
import { EmptyState } from "../components/EmptyState";
import { ResultCard } from "../components/ResultCard";
import { SourceStatusChip } from "../components/SourceStatusChip";

/**
 * Search, with live per-source status.
 *
 * The snapshot is polled rather than streamed. SSE is an accelerator that
 * carries nothing the snapshot lacks, and it is first on the cut list
 * (risks.md R13) precisely because losing it costs immediacy and nothing else.
 */
export function SearchPage() {
  const [query, setQuery] = useState("acme renewal");
  const [searchId, setSearchId] = useState<string | undefined>(undefined);
  const createSearch = useCreateSearch();
  const snapshot = useSearch(searchId);

  return (
    <section>
      <form
        className="searchbar"
        onSubmit={(event) => {
          event.preventDefault();
          createSearch.mutate(query, {
            onSuccess: (created) => setSearchId(created.search_id),
          });
        }}
      >
        <input
          aria-label="Search query"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search everything…"
        />
        <button type="submit" disabled={createSearch.isPending}>
          {createSearch.isPending ? "Searching…" : "Search"}
        </button>
      </form>

      {createSearch.isError ? (
        <p className="bad">{String(createSearch.error)}</p>
      ) : null}

      {snapshot.data ? (
        <>
          <div className="chips">
            {snapshot.data.sources.map((source) => (
              <SourceStatusChip key={source.source} source={source} />
            ))}
            {!snapshot.data.finished ? (
              <span className="muted">· partial results, still working</span>
            ) : null}
          </div>

          {snapshot.data.results.length === 0 ? (
            <EmptyState>
              {snapshot.data.finished
                ? "No matches. Check the source chips above — a source that could not be reached is not the same as one with nothing in it."
                : "Waiting for the first source to report…"}
            </EmptyState>
          ) : (
            <div className="results">
              {snapshot.data.results.map((result) => (
                <ResultCard key={`${result.source}:${result.id}`} result={result} />
              ))}
            </div>
          )}
        </>
      ) : (
        <EmptyState>Run a search to see results from every connected source.</EmptyState>
      )}
    </section>
  );
}
