/**
 * The API shapes the console consumes.
 *
 * ⏸️ Hand-written **this phase only**. `ui-architecture.md` specifies
 * `schema.d.ts` generated from `/openapi.json` by `openapi-typescript`, with a
 * build-time check that a drifted `Result` or `Draft` fails the build. That
 * belongs with group 11 and is on the outstanding list; until then the two
 * shapes the brief fixes are transcribed here verbatim so the drift, if any, is
 * at least visible in one file.
 */

/** CLOSED. No score, no raw, no extras — see design D7. */
export interface Result {
  source: string;
  id: string;
  title: string;
  snippet: string;
  author?: string;
  timestamp?: string;
  url: string;
}

export interface Draft {
  id: string;
  channel: "gmail" | "slack";
  to: string;
  subject?: string | null;
  body: string;
  idempotency_key: string;
}

export interface Confirmation {
  channel: "gmail" | "slack";
  recipient_display: string;
  connection_display: string;
  connection_id: number;
  subject: string | null;
  body: string;
  confirm_sha256: string;
  warning: string;
}

export type SourceStatus =
  | "pending"
  | "running"
  | "done"
  | "failed"
  | "needs_reconnect";

export interface SourceView {
  source: string;
  status: SourceStatus;
  mode: "live" | "mock" | "degraded";
  result_count: number;
  error?: {
    code: string;
    classification: string;
    message: string;
    reconnect_url?: string;
  };
}

export interface SearchSnapshot {
  search_id: string;
  query: string;
  is_seed: boolean;
  finished: boolean;
  sources: SourceView[];
  results: Result[];
}

export type SendState =
  | "in_flight"
  | "delivered"
  | "failed_transient"
  | "failed_permanent"
  | "uncertain";

export interface SendView {
  send_id: string;
  state: SendState;
  idempotent_replay: boolean;
  attempts: number;
  max_attempts?: number;
  provider_message_id: string | null;
  delivered_at: string | null;
  dispatched_at: string | null;
  reconcile_attempts: number;
  channel?: string;
  recipient_display?: string;
  connection_display?: string;
  subject?: string | null;
  body?: string;
  created_at?: string;
  is_seed?: boolean;
  retryable_by_operator?: boolean;
  error?: { classification: string; detail: string | null };
  uncertainty?: {
    dispatched_at: string | null;
    reconcile_attempts: number;
    reason: string | null;
    verify_url: string;
    resolutions: string[];
  };
}

export interface Connection {
  id: number;
  provider: string;
  display_name: string;
  status: string;
  last_error_detail: string | null;
}
