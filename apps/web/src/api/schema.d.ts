/**
 * Generated from the API's OpenAPI schema. DO NOT EDIT.
 *
 * Regenerate with `make schema`. A hand edit fails
 * `tests/test_schema_conformance.py`, which regenerates and compares —
 * because the point of generating these is that the client's idea of a
 * shape and the server's cannot drift apart silently.
 */

export interface components {
  schemas: {
    Confirmation: {
      channel: string;
      recipient_display: string;
      connection_display: string;
      connection_id: number;
      subject?: string | null;
      body: string;
      confirm_sha256: string;
      warning: string;
    };
    CreateDraftRequest: {
      channel: components["schemas"]["ProviderKind"];
      to: string;
      body: string;
      subject?: string | null;
      connection_id?: number | null;
      in_reply_to_result_id?: number | null;
    };
    CreateKeyRequest: {
      name?: string;
    };
    CreateSearchRequest: {
      query: string;
      sources?: string[] | null;
    };
    DevLoginRequest: {
      email?: string;
      name?: string;
    };
    Draft: {
      id: string;
      channel: string;
      to: string;
      subject?: string | null;
      body: string;
      idempotency_key: string;
    };
    DraftEnvelope: {
      draft: components["schemas"]["Draft"];
      confirmation: components["schemas"]["Confirmation"];
    };
    HTTPValidationError: {
      detail?: components["schemas"]["ValidationError"][];
    };
    PatchDraftRequest: {
      to?: string | null;
      subject?: string | null;
      body?: string | null;
    };
    ProviderKind: "gmail" | "slack";
    ResolveRequest: {
      resolution: string;
      note?: string | null;
    };
    Result: {
      source: string;
      id: string;
      title: string;
      snippet: string;
      author?: string | null;
      timestamp?: string | null;
      url: string;
    };
    SearchResults: {
      search_id: string;
      finished: boolean;
      results: components["schemas"]["Result"][];
    };
    SearchSnapshot: {
      search_id: string;
      query: string;
      is_seed: boolean;
      finished: boolean;
      sources: components["schemas"]["SourceStatus"][];
      results: components["schemas"]["Result"][];
      ranking?: Record<string, unknown>[] | null;
    };
    SendRequestBody: {
      confirmed_sha256?: string | null;
    };
    SourceError: {
      code: string;
      classification: string;
      message?: string | null;
      action_url?: string | null;
      reconnect_url?: string | null;
    };
    SourceStatus: {
      source: string;
      status: string;
      mode: string;
      result_count: number;
      connection_id?: number | null;
      display_name?: string | null;
      error?: components["schemas"]["SourceError"] | null;
    };
    ValidationError: {
      loc: (string | number)[];
      msg: string;
      type: string;
      input?: unknown;
      ctx?: Record<string, unknown>;
    };
  };
}

export type Result = components["schemas"]["Result"];
export type Draft = components["schemas"]["Draft"];
