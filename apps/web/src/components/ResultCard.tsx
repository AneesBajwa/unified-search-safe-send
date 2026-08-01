import { Link } from "react-router-dom";
import type { Result } from "../api/types";
import { relativeTime, sourceLabel } from "../lib/format";

/**
 * Renders a result without knowing where it came from — the criterion the
 * closed `Result` shape exists to serve. `source` is a label here, never a
 * branch, and there is no field on this card that a fourth source would have to
 * be taught about.
 *
 * "Draft a message" rather than "Reply", and the difference is honesty: the
 * console cannot thread a reply, because threading needs the internal result id
 * and `Result` is closed. Gmail reply threading is the first item on the cut
 * list (`risks.md` R13), so the button says what it does.
 */
export function ResultCard({ result }: { result: Result }) {
  return (
    <article className="card result">
      <header className="result-head">
        <span className="result-source">{sourceLabel(result.source)}</span>
        <span className="result-time">{relativeTime(result.timestamp)}</span>
      </header>
      <h3 className="result-title">{result.title}</h3>
      <p className="result-snippet">{result.snippet}</p>
      <footer className="result-foot">
        {result.author ? <span className="result-author">{result.author}</span> : null}
        <span className="result-actions">
          <a
            className="button button-quiet"
            href={result.url}
            target="_blank"
            rel="noreferrer"
          >
            Open
          </a>
          <Link
            className="button button-quiet"
            to={`/compose?subject=${encodeURIComponent(`Re: ${result.title}`)}`}
          >
            Draft a message
          </Link>
        </span>
      </footer>
    </article>
  );
}
