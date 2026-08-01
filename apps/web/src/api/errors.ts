/** The envelope every refusal arrives in (`api-design.md` §Errors). */
export interface ApiErrorBody {
  error?: {
    code: string;
    classification: "transient" | "permanent" | "needs_reconnect" | "config";
    message: string;
    /** The general name for "there is something to do about this". */
    action_url?: string;
    /** The revoked-grant case keeps its original name alongside `action_url`. */
    reconnect_url?: string;
  };
}

/**
 * The codes the console branches on, spelled once.
 *
 * The catalog in `apps/api/catalog.py` is the source of truth; these are the
 * subset that changes what the customer is *offered*, as opposed to what they
 * are told. Anything not listed renders as its message and nothing more, which
 * is the correct default: inventing an action for a code we do not understand
 * is how a customer ends up clicking something that cannot help them.
 */
export const ERROR_CODES = {
  /** The grant existed and was revoked. Re-grant in place. */
  needsReconnect: "connection_needs_reconnect",
  /** Never connected. A *first* connect, and a different verb. */
  notConnected: "connection_not_connected",
  /** We could not look, and it is not the customer's to fix. */
  providerUnavailable: "provider_unavailable",
  /** The draft moved under a held digest. Re-read and confirm again. */
  bodyChanged: "body_changed_since_confirmation",
  /** No digest was sent. The refusal the whole product is built around. */
  confirmationRequired: "confirmation_required",
  /** An in-doubt send needs a decision, never a retry. */
  resolutionRequired: "resolution_required",
  /** Our bug, not theirs: no OAuth client is configured for that provider. */
  notConfigured: "provider_not_configured",
} as const;
