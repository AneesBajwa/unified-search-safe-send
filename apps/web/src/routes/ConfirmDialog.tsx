import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { ApiError } from "../api/client";
import { ERROR_CODES } from "../api/errors";
import { useDraft, useSendDraft } from "../api/hooks";
import { StateBadge } from "../components/StateBadge";
import { useConnectFlow } from "../lib/useConnectFlow";

/**
 * **The gate, as the customer meets it.** The highest-value component in the
 * app, and the one the product is judged on: a cramped or ambiguous confirm
 * step *is* the misfire this whole thing exists to prevent.
 *
 * Five requirements, all from customer risk rather than aesthetics
 * (`ui-architecture.md` §ConfirmDialog):
 *
 * 1. **The destination is the most prominent element**, above the body, echoed
 *    verbatim — never a friendly alias that could hide the real recipient.
 * 2. **The full body is visible and scrollable**, never truncated with an
 *    ellipsis. A customer cannot confirm what they cannot read, so the body owns
 *    the only scrolling region on the screen and the destination stays pinned
 *    above it.
 * 3. **Confirm and Cancel are visually distinct and physically separated.** On a
 *    phone Cancel is full-width at the bottom — where a thumb rests — and
 *    Confirm sits above it with a 24px gap. They are never adjacent, because
 *    mistapping is the failure mode this product exists to prevent.
 * 4. **Confirm disables on press and immediately shows the returned send
 *    state**, identified by id. Never an indeterminate spinner: a customer who
 *    cannot tell whether it worked will press again, and that instinct is
 *    exactly the double-tap the gate defends against (`risks.md` R16).
 * 5. **Correct at 375px**, which is why it is a full-screen sheet on a phone and
 *    a centred dialog only from 40rem up.
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
  const connect = useConnectFlow();
  const [pressed, setPressed] = useState(false);
  // Belt as well as braces. `disabled` depends on a re-render landing between
  // two taps, and the gap between a double-tap's two `click` events is roughly
  // the gap this defends against — so the second press is refused here, before
  // React is involved at all. The API is idempotent by the draft's key either
  // way; this is about the customer never seeing a second request happen.
  const inFlight = useRef(false);

  const settled = send.data;

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      // Escape cancels, never sends. The safe direction is the only one a
      // stray keystroke is allowed to take.
      if (event.key === "Escape" && !settled) navigate(-1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [navigate, settled]);

  if (draft.isLoading) {
    return (
      <div className="gate">
        <div className="gate-sheet gate-loading">
          <p className="muted">Reading the draft…</p>
        </div>
      </div>
    );
  }

  if (draft.isError || !draft.data) {
    return (
      <div className="gate">
        <div className="gate-sheet gate-loading">
          <p className="notice-title">That draft could not be read</p>
          <p className="notice-detail">{String(draft.error)}</p>
          <button type="button" className="button" onClick={() => navigate("/compose")}>
            Back to compose
          </button>
        </div>
      </div>
    );
  }

  const { confirmation } = draft.data;
  const error = send.error instanceof ApiError ? send.error : null;

  return (
    <div className="gate">
      <section
        className="gate-sheet"
        role="dialog"
        aria-modal="true"
        aria-labelledby="gate-destination"
      >
        {/* 1. Destination first, largest, and pinned — it never scrolls away. */}
        <header className="gate-head">
          <p className="gate-eyebrow">Confirm before sending</p>
          <p className="gate-destination" id="gate-destination">
            {confirmation.recipient_display}
          </p>
          <p className="gate-warning">{confirmation.warning}</p>
          <p className="gate-via">
            <span className="gate-channel">{confirmation.channel}</span>
            <span aria-hidden="true"> · </span>
            from {confirmation.connection_display}
          </p>
          {confirmation.subject ? (
            <p className="gate-subject">
              <span className="gate-label">Subject</span> {confirmation.subject}
            </p>
          ) : null}
        </header>

        {/* 2. The full body, scrollable, never truncated. */}
        <div className="gate-scroll">
          <p className="gate-label gate-body-label">Message</p>
          <div className="body-preview">{confirmation.body}</div>

          {error ? (
            <div className="notice notice-bad gate-error">
              <div className="notice-body">
                <p className="notice-title">This was refused</p>
                <p className="notice-detail">{error.message}</p>
              </div>
              {error.code === ERROR_CODES.bodyChanged ? (
                <button
                  type="button"
                  className="button button-primary"
                  onClick={() => {
                    inFlight.current = false;
                    setPressed(false);
                    send.reset();
                    void draft.refetch();
                  }}
                >
                  Re-read the draft and confirm again
                </button>
              ) : null}
              {error.actionUrl ? (
                <button
                  type="button"
                  className="button button-primary"
                  disabled={connect.phase === "waiting"}
                  onClick={() => connect.start(error.actionUrl as string)}
                >
                  {connect.phase === "waiting" ? "Waiting for the grant…" : "Reconnect"}
                </button>
              ) : null}
            </div>
          ) : null}

          <p className="gate-digest">
            Confirming <code>{confirmation.confirm_sha256.slice(0, 12)}…</code> — a digest
            over the channel, the recipient, the subject and the body. Edit the draft and
            this confirmation stops being valid.
          </p>
        </div>

        {/* 3 and 4. Distinct, separated, disabled on press, and the returned
            send state shown immediately rather than a spinner.

            The settled block is its own region rather than a child of the
            action row. As a grid item inside a column-flow parent it collapsed
            into a narrow right-pinned column with dead canvas beside it; out
            here it cannot. */}
        {settled ? (
          <>
            <div className="gate-settled">
              <p className="gate-settled-line">
                <StateBadge state={settled.send.state} />
                <span className="gate-settled-id">
                  <code>{settled.send.send_id.slice(0, 8)}</code>
                </span>
              </p>
              <p className="gate-settled-note">
                {settled.replayed
                  ? "This was a duplicate — the same message, not a second one."
                  : "Recorded. It is now yours to follow."}
              </p>
            </div>
            <footer className="gate-actions">
              <button
                type="button"
                className="button button-lg"
                onClick={() => navigate("/history")}
              >
                Done
              </button>
              <button
                type="button"
                className="button button-primary button-lg"
                onClick={() => navigate(`/history/sends/${settled.send.send_id}`)}
              >
                Open the send
              </button>
            </footer>
          </>
        ) : (
          <footer className="gate-actions">
            <button
              type="button"
              className="button button-lg"
              onClick={() => navigate(-1)}
            >
              Cancel
            </button>
            <button
              type="button"
              className="button button-primary button-lg"
              disabled={pressed || send.isPending}
              onClick={() => {
                if (inFlight.current) return;
                inFlight.current = true;
                setPressed(true);
                send.mutate({
                  draftId: draft.data.draft.id,
                  confirmedSha256: confirmation.confirm_sha256,
                });
              }}
            >
              {pressed ? "Sending…" : "Send it"}
            </button>
          </footer>
        )}
      </section>
    </div>
  );
}
