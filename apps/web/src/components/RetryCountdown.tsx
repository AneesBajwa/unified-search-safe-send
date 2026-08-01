import type { SendView } from "../api/types";

/**
 * "Attempt 3 of 6", never an indeterminate spinner.
 *
 * A customer watching a spinner cannot tell progress from a hang, and the
 * instinct a hang produces is to press the button again — which is exactly the
 * double-tap the gate defends against.
 */
export function RetryCountdown({ send }: { send: SendView }) {
  if (send.state !== "in_flight") return null;
  const max = send.max_attempts ?? 6;
  return (
    <span className="muted">
      attempt {Math.max(send.attempts, 1)} of {max}
      {send.reconcile_attempts > 0
        ? ` · reconciling (${send.reconcile_attempts}/3)`
        : ""}
    </span>
  );
}
