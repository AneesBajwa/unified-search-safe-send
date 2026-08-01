import { Link } from "react-router-dom";
import type { Result } from "../api/types";
import { relativeTime, sourceLabel } from "../lib/format";

/**
 * Renders a result without knowing where it came from — which is the criterion
 * the closed `Result` shape exists to serve. `source` is a label here, never a
 * branch.
 */
export function ResultCard({ result }: { result: Result }) {
  return (
    <article className="card result">
      <header>
        <span className="source">{sourceLabel(result.source)}</span>
        <span className="muted">{relativeTime(result.timestamp)}</span>
      </header>
      <h3>{result.title}</h3>
      <p className="snippet">{result.snippet}</p>
      <footer>
        {result.author ? <span className="muted">{result.author}</span> : null}
        <a href={result.url} target="_blank" rel="noreferrer">
          open
        </a>
        <Link className="button" to={`/compose/${encodeURIComponent(result.id)}`}>
          Reply
        </Link>
      </footer>
    </article>
  );
}
