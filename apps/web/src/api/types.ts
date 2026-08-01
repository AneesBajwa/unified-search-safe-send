/**
 * The API shapes the console consumes.
 *
 * 🔴 **Everything the generator emits is re-exported from `schema.d.ts`, never
 * restated here.** That file is produced by `make schema` from `/openapi.json`,
 * and `tests/test_schema_conformance.py` regenerates and byte-compares it — so a
 * `Result` or `Draft` that drifts fails the build. Phase 2 transcribed those
 * shapes by hand with a docstring admitting it; task 11.1 retires that, because
 * a transcription kept "for readability" beside a generated file is exactly the
 * drift the generator exists to prevent.
 *
 * What is still hand-written, and why: three routes return `dict[str, Any]` from
 * FastAPI, so OpenAPI has nothing to describe and the generator emits nothing.
 * Those are the **listing envelopes**, the **connection** row and the **send**
 * view, all marked below. Narrowing any of them to a literal union would be
 * worse than leaving it loose — an API that grows a state the console has never
 * heard of should render it plainly rather than crash.
 */

import type { components } from "./schema.d.ts";

type Schemas = components["schemas"];

// ---------------------------------------------------------------- generated

/** CLOSED. No score, no raw, no extras — see design D7. */
export type Result = Schemas["Result"];
export type Draft = Schemas["Draft"];
export type Confirmation = Schemas["Confirmation"];
export type DraftEnvelope = Schemas["DraftEnvelope"];
export type SearchSnapshot = Schemas["SearchSnapshot"];
export type SearchResults = Schemas["SearchResults"];
export type SourceView = Schemas["SourceStatus"];
export type SourceError = Schemas["SourceError"];
export type ProviderKind = Schemas["ProviderKind"];
export type CreateDraftRequest = Schemas["CreateDraftRequest"];

// -------------------------------------------------- hand-written, and why

/**
 * ⏸️ Hand-written: `GET /sends/{id}` returns `dict[str, Any]`, so OpenAPI
 * describes it as an object with no properties and the generator emits nothing
 * usable. Every field below was read off a live response rather than off a
 * model — `send_detail` in `core/send/service.py` is the authority.
 */
export interface SendView {
  send_id: string;
  /** `in_flight` | `delivered` | `failed_transient` | `failed_permanent` | `uncertain` */
  state: string;
  idempotent_replay: boolean;
  attempts: number;
  max_attempts?: number;
  provider_message_id: string | null;
  delivered_at: string | null;
  dispatched_at: string | null;
  reconcile_attempts: number;
  /** Only while a retry is genuinely waiting. A terminal failure carries null. */
  next_attempt_at?: string | null;
  /** Full jitter, so the countdown is read from here and never computed. */
  backoff_seconds?: number | null;
  channel?: string;
  recipient_display?: string;
  connection_display?: string;
  subject?: string | null;
  body?: string;
  created_at?: string;
  is_seed?: boolean;
  /** The API's decision, never the console's. */
  retryable_by_operator?: boolean;
  error?: { classification: string; detail: string | null };
  /** Present only while `state === "uncertain"`. */
  uncertainty?: {
    dispatched_at: string | null;
    reconcile_attempts: number;
    reason: string | null;
    verify_url: string;
    resolutions: string[];
  };
  /**
   * Present once a person has settled an in-doubt send. Rendered as *their*
   * claim, never as the provider's — which is the whole content of this state.
   */
  resolution?: { resolution: string; note: string | null; created_at: string };
}

/** ⏸️ Hand-written for the same reason: `GET /connections` returns a dict. */
export interface Connection {
  id: number;
  provider: string;
  display_name: string;
  /** `active` | `needs_reconnect` | … */
  status: string;
  last_error_detail: string | null;
  granted_scopes?: string[];
  last_success_at?: string | null;
  /** Reported, never enforced — a wrong hint costs a banner (`scopes.py`). */
  scopes_ok?: boolean;
  missing_scopes?: string[];
  /** Present when re-granting is the fix. Followed, never rebuilt. */
  reconnect_url?: string;
}

/** Which providers can be connected at all right now. */
export interface AvailableProvider {
  provider: string;
  configured: boolean;
  /** Null when there is no OAuth client — our gap, not the customer's. */
  authorize_url: string | null;
  /** Shown at the moment of asking, not only in the README (task 8.2d). */
  rationale: string;
}

export interface ConnectionsResponse {
  connections: Connection[];
  available: AvailableProvider[];
}

export interface AuthorizeResponse {
  authorize_url: string;
  provider: string;
  rationale: string;
}

export interface SearchListRow {
  search_id: string;
  query: string;
  is_seed: boolean;
  created_at: string;
  finished: boolean;
  result_count: number;
}
