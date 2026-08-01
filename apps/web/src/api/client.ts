/**
 * One fetch wrapper. Every call to the API goes through it.
 *
 * Two rules the UI inherits from having exactly one door:
 *
 * - The key rides an `X-API-Key` **header**, never a query string. Cloud Run
 *   logs every request URL to Cloud Logging by default, where it sits for the
 *   bucket's retention period readable by anyone with project Viewer — an
 *   unacceptable place for a credential that grants access to a user's
 *   connected accounts (risks.md R7).
 * - The error envelope is turned into a typed `ApiError` here, so no component
 *   ever parses an error body. Whether something is retryable is the API's
 *   decision, carried on `classification`.
 */

import type { ApiErrorBody } from "./errors";

const API_BASE_URL: string =
  import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8080";

const KEY_STORAGE = "usss.api_key";

export class ApiError extends Error {
  // Plain fields rather than constructor parameter properties: the app builds
  // with `erasableSyntaxOnly`, which rejects any TypeScript that emits runtime
  // code.
  status: number;
  code: string;
  classification: string;
  reconnectUrl?: string;

  constructor(
    status: number,
    code: string,
    classification: string,
    message: string,
    reconnectUrl?: string,
  ) {
    super(message);
    this.status = status;
    this.code = code;
    this.classification = classification;
    this.reconnectUrl = reconnectUrl;
  }
}

export function storedKey(): string | null {
  return sessionStorage.getItem(KEY_STORAGE);
}

export function storeKey(key: string): void {
  sessionStorage.setItem(KEY_STORAGE, key);
}

/**
 * The PoC sign-in. Mints a key on first load so the product is clickable
 * without an auth flow that does not exist yet — group 6 replaces this with
 * real OAuth and the rest of the console does not change.
 */
export async function ensureKey(): Promise<string> {
  const existing = storedKey();
  if (existing) return existing;
  const response = await fetch(`${API_BASE_URL}/v1/auth/dev-login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email: "console@example.test" }),
  });
  if (!response.ok) throw new Error(`sign-in failed: ${response.status}`);
  const body = (await response.json()) as { key: string };
  storeKey(body.key);
  return body.key;
}

export interface RequestOptions {
  method?: string;
  body?: unknown;
}

export interface Envelope<T> {
  data: T;
  headers: Headers;
}

export async function api<T>(path: string, options: RequestOptions = {}): Promise<T> {
  return (await apiWithHeaders<T>(path, options)).data;
}

/**
 * For the one call whose contract puts something on a header: the send gate
 * returns `Idempotent-Replayed: true|false` alongside the body.
 */
export async function apiWithHeaders<T>(
  path: string,
  options: RequestOptions = {},
): Promise<Envelope<T>> {
  const key = await ensureKey();
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: options.method ?? "GET",
    headers: {
      "X-API-Key": key,
      ...(options.body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });

  const text = await response.text();
  const payload = text ? JSON.parse(text) : {};

  if (!response.ok) {
    const envelope = payload as ApiErrorBody;
    const error = envelope?.error;
    throw new ApiError(
      response.status,
      error?.code ?? "unknown",
      error?.classification ?? "permanent",
      error?.message ?? `${response.status} ${response.statusText}`,
      error?.reconnect_url,
    );
  }

  return { data: payload as T, headers: response.headers };
}
