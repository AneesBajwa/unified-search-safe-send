import { useEffect, useState } from "react";
import type { SendView } from "../api/types";
import { secondsUntil } from "../lib/format";

/**
 * "Retrying in 12s · attempt 3 of 6" — a real countdown, never a spinner.
 *
 * A customer watching a spinner cannot tell progress from a hang, and the
 * instinct a hang produces is to press the button again, which is exactly the
 * double-tap the gate defends against (`risks.md` R16).
 *
 * 🔴 **The clock is read, not computed.** The backoff has full jitter, so the
 * client cannot derive when the next attempt lands — `next_attempt_at` and
 * `backoff_seconds` exist on the payload for precisely this, and they are
 * populated only while a retry is genuinely waiting. A terminal failure carries
 * null and gets no countdown, because a countdown that never fires is a worse
 * lie than a spinner: it looks like information.
 */
export function RetryCountdown({ send }: { send: SendView }) {
  const target = send.next_attempt_at ?? null;
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!target) return;
    const tick = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(tick);
  }, [target]);

  if (send.state !== "in_flight") return null;

  const attempts = `attempt ${Math.max(send.attempts, 1)} of ${send.max_attempts ?? 6}`;

  if (send.reconcile_attempts > 0) {
    // A different question from "when do we try again": this one is "did the
    // last attempt land", and saying so is what keeps a customer from reading
    // the delay as the message being stuck.
    return (
      <span className="countdown countdown-checking">
        checking whether it arrived ({send.reconcile_attempts} of 3)
      </span>
    );
  }

  if (!target) return <span className="countdown">{attempts}</span>;

  const remaining = secondsUntil(target, now);
  return (
    <span className="countdown countdown-waiting">
      {remaining > 0 ? `retrying in ${remaining}s` : "retrying now"}
      <span className="countdown-sep"> · </span>
      {attempts}
      {send.backoff_seconds ? (
        <span className="countdown-backoff"> (backoff {send.backoff_seconds}s)</span>
      ) : null}
    </span>
  );
}
