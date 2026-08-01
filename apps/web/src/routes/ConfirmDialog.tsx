import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { ApiError } from "../api/client";
import { useDraft, useSendDraft } from "../api/hooks";

/**
 * **The gate, as the customer meets it.** The highest-value component in the app.
 *
 * Requirements, all from the customer's risk rather than from aesthetics
 * (ui-architecture.md):
 *
 * 1. The destination is the most prominent element, above the body. Never a
 *    friendly alias that could hide the real recipient.
 * 2. The full body is visible and scrollable, never truncated with an ellipsis.
 *    A customer cannot confirm what they cannot read.
 * 3. Confirm and Cancel are visually distinct and physically separated. On a
 *    phone Cancel is full-width at the bottom with a ≥24px gap above it, so the
 *    two are never adjacent — mistapping is the failure mode this product
 *    exists to prevent.
 * 4. Confirm disables itself on press and shows the returned send state
 *    immediately. It never spins indeterminately: that instinct — "did it
 *    work? press again" — is exactly the double-tap the gate defends against.
 *
 * The dialog contains **no logic beyond rendering `confirmation` and posting
 * `confirmed_sha256`**. Whether the send may proceed is the API's decision.
 * `confirm_sha256` is held in memory only; a reload re-fetches the draft and
 * re-renders, so the customer always confirms something they can currently see.
 */
export function ConfirmDialog() {
  const { draftId } = useParams<{ draftId: string }>();
  const navigate = useNavigate();
  const draft = useDraft(draftId);
  const send = useSendDraft();
  const [pressed, setPressed] = useState(false);

  if (draft.isLoading) return <p className="muted">Loading the draft…</p>;
  if (draft.isError || !draft.data) return <p className="bad">{String(draft.error)}</p>;

  const { confirmation } = draft.data;
  const error = send.error instanceof ApiError ? send.error : null;

  return (
    <section className="confirm">
      <h2>Confirm before sending</h2>

      {/* 1. Destination first, and largest. */}
      <p className="destination">{confirmation.warning}</p>
      <dl className="destination-detail">
        <dt>Channel</dt>
        <dd>{confirmation.channel}</dd>
        <dt>To</dt>
        <dd>
          <strong>{confirmation.recipient_display}</strong>
        </dd>
        <dt>Account</dt>
        <dd>{confirmation.connection_display}</dd>
        {confirmation.subject ? (
          <>
            <dt>Subject</dt>
            <dd>{confirmation.subject}</dd>
          </>
        ) : null}
      </dl>

      {/* 2. The full body, scrollable, never truncated. */}
      <div className="body-preview">{confirmation.body}</div>

      {error ? (
        <div className="bad">
          <p>{error.message}</p>
          {error.code === "body_changed_since_confirmation" ? (
            <button type="button" onClick={() => void draft.refetch()}>
              Re-read the draft and confirm again
            </button>
          ) : null}
          {error.reconnectUrl ? (
            <a href="/connections">Reconnect this account</a>
          ) : null}
        </div>
      ) : null}

      {send.data ? (
        <div className="sent">
          <p>
            Send <code>{send.data.send.send_id}</code> is{" "}
            <strong>{send.data.send.state}</strong>
            {send.data.replayed
              ? " (this was a duplicate — nothing was sent twice)"
              : ""}
          </p>
          <button
            type="button"
            onClick={() => navigate(`/history/sends/${send.data.send.send_id}`)}
          >
            Open the send
          </button>
        </div>
      ) : null}

      {/* 3 and 4. Distinct, separated, and disabled on press. */}
      <div className="confirm-actions">
        <button
          type="button"
          className="primary"
          disabled={pressed || send.isPending || Boolean(send.data)}
          onClick={() => {
            setPressed(true);
            send.mutate({
              draftId: draft.data.draft.id,
              confirmedSha256: confirmation.confirm_sha256,
            });
          }}
        >
          {send.data ? `Sent · ${send.data.send.state}` : pressed ? "Sending…" : "Send it"}
        </button>
        <div className="gap" />
        <button type="button" className="cancel" onClick={() => navigate(-1)}>
          Cancel
        </button>
      </div>

      <p className="muted digest">
        Confirming <code>{confirmation.confirm_sha256.slice(0, 12)}…</code> — a digest over
        the channel, the recipient, the subject and the body. Edit the draft and this
        confirmation stops being valid.
      </p>
    </section>
  );
}
