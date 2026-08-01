import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { ApiError } from "../api/client";
import { useResolveSend, useRetrySend, useSend } from "../api/hooks";
import { SeedBadge } from "../components/EmptyState";
import { RetryCountdown } from "../components/RetryCountdown";
import { StateBadge } from "../components/StateBadge";
import { absoluteTime, resolutionLabel, sourceLabel } from "../lib/format";

/**
 * The screen that matters once something has already gone wrong.
 *
 * It carries the full provider error rather than a summary, the attempt count,
 * and — for an in-doubt send — the evidence a person needs to settle it
 * themselves: when we dispatched, how many times we tried to reconcile, and
 * where to go and look.
 *
 * 🔴 **An operator retry is offered only when the API says so.** `uncertain`
 * gets resolution choices instead, because a generic retry there may re-send a
 * message that already arrived — the exact misfire this product exists to
 * prevent. `retryable_by_operator` is read; it is never derived from the state.
 */
export function SendDetailPage() {
  const { sendId } = useParams<{ sendId: string }>();
  const send = useSend(sendId);
  const retry = useRetrySend();

  if (send.isLoading) return <p className="muted">Loading…</p>;
  if (send.isError || !send.data) {
    return (
      <section className="page">
        <div className="notice notice-bad">
          <div className="notice-body">
            <p className="notice-title">That send could not be read</p>
            <p className="notice-detail">{String(send.error)}</p>
          </div>
        </div>
        <Link className="button" to="/history">
          Back to history
        </Link>
      </section>
    );
  }

  const detail = send.data;
  const retryError = retry.error instanceof ApiError ? retry.error : null;

  return (
    <section className="page">
      <header className="page-head detail-head">
        <div className="detail-state">
          <StateBadge state={detail.state} />
          <SeedBadge isSeed={detail.is_seed} />
        </div>
        <h2 className="detail-recipient">{detail.recipient_display}</h2>
        <p className="page-sub">
          {detail.channel ? sourceLabel(detail.channel) : ""}
          {detail.connection_display ? ` · from ${detail.connection_display}` : ""}
        </p>
        {detail.subject ? <p className="detail-subject">{detail.subject}</p> : null}
      </header>

      <dl className="facts">
        <div className="fact">
          <dt>Attempts</dt>
          <dd>
            {detail.attempts} of {detail.max_attempts ?? 6} <RetryCountdown send={detail} />
          </dd>
        </div>
        <div className="fact">
          <dt>Dispatched</dt>
          <dd>{absoluteTime(detail.dispatched_at)}</dd>
        </div>
        <div className="fact">
          <dt>Delivered</dt>
          <dd>{absoluteTime(detail.delivered_at)}</dd>
        </div>
        <div className="fact">
          <dt>Provider message id</dt>
          <dd>
            <code>{detail.provider_message_id ?? "—"}</code>
          </dd>
        </div>
      </dl>

      <h3 className="section-head">Exactly what was transmitted</h3>
      <div className="body-preview">{detail.body}</div>

      {detail.resolution ? (
        <div className="notice notice-info">
          <div className="notice-body">
            {/* "You marked this delivered", never "the provider confirmed it".
                They are different claims, and in this state the difference is
                the entire content. */}
            <p className="notice-title">{resolutionLabel(detail.resolution.resolution)}</p>
            <p className="notice-detail">
              {absoluteTime(detail.resolution.created_at)}
              {detail.resolution.note ? ` — ${detail.resolution.note}` : ""}
            </p>
            <p className="notice-meta">
              Recorded from your decision, not from the provider. Nothing here is a
              delivery receipt.
            </p>
          </div>
        </div>
      ) : null}

      {detail.error ? (
        <>
          <h3 className="section-head">What went wrong</h3>
          <p className="muted">classification: {detail.error.classification}</p>
          {/* Untruncated on purpose: a generic message is what makes an operator
              guess, and this is the only evidence they have. */}
          <pre className="error-detail">{detail.error.detail}</pre>
        </>
      ) : null}

      {detail.uncertainty ? (
        <UncertaintyPanel sendId={detail.send_id} uncertainty={detail.uncertainty} />
      ) : null}

      <div className="detail-actions">
        {detail.retryable_by_operator ? (
          <button
            type="button"
            className="button button-primary"
            disabled={retry.isPending}
            onClick={() => retry.mutate(detail.send_id)}
          >
            {retry.isPending ? "Retrying…" : "Retry under the same key"}
          </button>
        ) : null}
        <Link className="button" to="/history">
          Back to history
        </Link>
      </div>

      {retryError ? <p className="bad">{retryError.message}</p> : null}
    </section>
  );
}

/**
 * `uncertain` is amber, not red, and it is the one state whose copy has to do
 * real work: failed means we know nothing was sent, uncertain means we do not
 * know. Conflating them invites retrying a message that already went out.
 *
 * The resolutions offered are whatever `uncertainty.resolutions` carries. The
 * console never invents one and never offers a retry here.
 */
function UncertaintyPanel({
  sendId,
  uncertainty,
}: {
  sendId: string;
  uncertainty: NonNullable<import("../api/types").SendView["uncertainty"]>;
}) {
  const resolve = useResolveSend();
  const [note, setNote] = useState("");
  const [chosen, setChosen] = useState<string | null>(null);

  const COPY: Record<string, { label: string; help: string }> = {
    marked_delivered: {
      label: "I can see it — mark it delivered",
      help: "Records your attestation. No provider id is invented for it.",
    },
    forced_resend: {
      label: "It is not there — send it again",
      help: "The only path that clears the dispatch record, so the next attempt actually dispatches.",
    },
  };

  return (
    <div className="doubt">
      <h3 className="doubt-title">We do not know whether this arrived</h3>
      <p className="doubt-lede">
        It was dispatched at {absoluteTime(uncertainty.dispatched_at)} and we asked the
        provider {uncertainty.reconcile_attempts} times without getting a usable answer.
        This is not a failure — a failure would mean we know nothing was sent.
      </p>
      {uncertainty.reason ? <p className="doubt-reason">{uncertainty.reason}</p> : null}

      {/* Its own control rather than a link buried in a sentence: it is the
          action that actually settles this, and an inline link is a 15px-tall
          target on a phone. */}
      <a
        className="button doubt-verify"
        href={uncertainty.verify_url}
        target="_blank"
        rel="noreferrer"
      >
        Look for yourself at the provider
      </a>
      <p className="doubt-help">
        It takes about three seconds, and you can answer this in a way we cannot.
      </p>

      <label className="field">
        <span className="field-label">Note (optional)</span>
        <input
          value={note}
          onChange={(event) => setNote(event.target.value)}
          placeholder="what you saw"
        />
      </label>

      <div className="doubt-actions">
        {uncertainty.resolutions.map((resolution) => {
          const copy = COPY[resolution] ?? { label: resolution, help: "" };
          const armed = chosen === resolution;
          return (
            <div key={resolution} className="doubt-choice">
              <button
                type="button"
                className={armed ? "button button-primary" : "button"}
                disabled={resolve.isPending}
                onClick={() => {
                  if (!armed) {
                    setChosen(resolution);
                    return;
                  }
                  resolve.mutate({ sendId, resolution, note });
                }}
              >
                {armed
                  ? resolve.isPending
                    ? "Recording…"
                    : "Tap again to confirm"
                  : copy.label}
              </button>
              <p className="doubt-help">{copy.help}</p>
            </div>
          );
        })}
      </div>

      {resolve.isError ? <p className="bad">{String(resolve.error)}</p> : null}
    </div>
  );
}
