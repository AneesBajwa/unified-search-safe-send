import type { SourceView } from "../api/types";
import { sourceLabel } from "../lib/format";

/**
 * Per-source status as a standing panel rather than a chip row that scrolls
 * away.
 *
 * The reason is specific rather than aesthetic: a source that failed silently
 * makes an incomplete answer look complete, and in a single column that
 * information leaves the viewport the moment you scroll the results. This is
 * what the desktop width is spent on.
 *
 * The chip row still renders below 64rem — CSS decides which is visible, and
 * neither derives anything. `status`, `error.classification` and
 * `result_count` are read from the payload exactly as the chips read them.
 */
export function SourcePanel({
  sources,
  reported,
}: {
  sources: SourceView[];
  reported: number;
}) {
  return (
    <aside className="panel source-panel" aria-label="Source status">
      <p className="panel-head">
        Sources · {reported} of {sources.length} reported
      </p>
      <ul className="panel-list">
        {sources.map((source) => (
          <li
            key={`${source.source}:${source.connection_id ?? "none"}`}
            className="panel-item"
          >
            <div className="panel-row">
              <span className="source-panel-name">
                {source.display_name
                  ? `${sourceLabel(source.source)} · ${source.display_name}`
                  : sourceLabel(source.source)}
              </span>
              <SourceState source={source} />
            </div>
          </li>
        ))}
      </ul>
    </aside>
  );
}

/** The same four terminal states the chips render, in the same colours. */
function SourceState({ source }: { source: SourceView }) {
  if (source.status === "pending" || source.status === "running") {
    return (
      <span className="source-panel-state is-running">
        searching<span className="ellipsis" aria-hidden="true" />
      </span>
    );
  }
  if (source.status === "failed" || source.error) {
    const retrying = source.error?.classification === "transient";
    return (
      <span className={retrying ? "source-panel-state is-retrying" : "source-panel-state is-failed"}>
        {retrying ? "retrying" : "unavailable"}
      </span>
    );
  }
  if (source.result_count > 0) {
    return <span className="source-panel-state is-done">{source.result_count}</span>;
  }
  return <span className="source-panel-state">no matches</span>;
}
