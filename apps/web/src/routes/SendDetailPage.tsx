import { useParams } from "react-router-dom";
import { ApiError } from "../api/client";
import { useRetrySend, useSend } from "../api/hooks";
import { RetryCountdown } from "../components/RetryCountdown";
import { StateBadge } from "../components/StateBadge";

/**
 * The screen that matters once something has already gone wrong.
 *
 * It carries the full provider error rather than a summary, the attempt count,
 * and — for an in-doubt send — the evidence a person needs to settle it
 * themselves: when we dispatched, how many times we tried to reconcile, and
 * where to go and look. An operator retry is offered only for a terminal
 * *transient* failure; `uncertain` gets resolution choices instead, because a
 * generic retry there may send a message that already arrived.
 */
export function SendDetailPage() {
  const { sendId } = useParams<{ sendId: string }>();
  const send = useSend(sendId);
  const retry = useRetrySend();

  if (send.isLoading) return <p className="muted">Loading…</p>;
  if (send.isError || !send.data) return <p className="bad">{String(send.error)}</p>;

  const detail = send.data;
  const retryError = retry.error instanceof ApiError ? retry.error : null;

  return (
    <section>
      <h2>
        Send <StateBadge state={detail.state} />
      </h2>

      <dl>
        <dt>To</dt>
        <dd>{detail.recipient_display}</dd>
        <dt>Account</dt>
        <dd>{detail.connection_display}</dd>
        {detail.subject ? (
          <>
            <dt>Subject</dt>
            <dd>{detail.subject}</dd>
          </>
        ) : null}
        <dt>Attempts</dt>
        <dd>
          {detail.attempts} of {detail.max_attempts ?? 6} <RetryCountdown send={detail} />
        </dd>
        <dt>Provider message id</dt>
        <dd>
          <code>{detail.provider_message_id ?? "—"}</code>
        </dd>
        <dt>Delivered</dt>
        <dd>{detail.delivered_at ?? "—"}</dd>
      </dl>

      <h3>Exactly what was transmitted</h3>
      <div className="body-preview">{detail.body}</div>

      {detail.error ? (
        <>
          <h3>Error</h3>
          <p className="muted">classification: {detail.error.classification}</p>
          {/* Untruncated on purpose: a generic message is what makes an
              operator guess, and this is the only evidence they have. */}
          <pre className="error-detail">{detail.error.detail}</pre>
        </>
      ) : null}

      {detail.uncertainty ? (
        <div className="doubt">
          <h3>In doubt</h3>
          <p>
            Dispatched at {detail.uncertainty.dispatched_at}. Reconciled{" "}
            {detail.uncertainty.reconcile_attempts} times without an answer.
          </p>
          <p className="muted">{detail.uncertainty.reason}</p>
          <p>
            <a href={detail.uncertainty.verify_url} target="_blank" rel="noreferrer">
              Check at the provider
            </a>
          </p>
          <p className="muted">
            Resolutions: {detail.uncertainty.resolutions.join(", ")} — an explicit
            decision, never a generic retry. (The resolve endpoint lands with group 9.)
          </p>
        </div>
      ) : null}

      {detail.retryable_by_operator ? (
        <button
          type="button"
          disabled={retry.isPending}
          onClick={() => retry.mutate(detail.send_id)}
        >
          Retry under the same key
        </button>
      ) : null}
      {retryError ? <p className="bad">{retryError.message}</p> : null}
    </section>
  );
}
