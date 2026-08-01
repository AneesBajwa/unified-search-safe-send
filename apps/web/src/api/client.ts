/**
 * One fetch wrapper. Every call to the API goes through it.
 *
 * Three rules the UI inherits from having exactly one door:
 *
 * - The key rides an `X-API-Key` **header**, never a query string. Cloud Run
 *   logs every request URL to Cloud Logging by default, where it sits for the
 *   bucket's retention period readable by anyone with project Viewer — an
 *   unacceptable place for a credential that grants access to a user's
 *   connected accounts (`risks.md` R7). That applies to the SSE stream too,
 *   which is why it is `fetch` + `ReadableStream` and never `EventSource`.
 * - The error envelope is turned into a typed `ApiError` here, so no component
 *   ever parses an error body. Whether something is retryable is the API's
 *   decision, carried on `classification`.
 * - **Nothing is minted implicitly.** Phase 2 called `dev-login` on the first
 *   request, which meant the console could only ever be one user — and "sign in
 *   as a brand-new user and search before connecting anything" is a state the
 *   product has to be able to show. Signing in is now an explicit act.
 */

import type { ApiErrorBody } from "./errors";

/** The port the API is served on, everywhere except a forwarded environment. */
const API_PORT = "8080";
const SPA_PORT = "5173";

/**
 * Where the API lives, derived from where this page came from.
 *
 * Three environments, and only one of them can be hardcoded:
 *
 * - **Local and LAN** — same host, different port. Hardcoding `localhost` here
 *   is why a phone on the LAN fails: it calls *itself* and every request dies
 *   for no visible reason.
 * - **GitHub Codespaces** — 🔴 the port is in the **hostname**, not after a
 *   colon: `https://<name>-5173.app.github.dev`. So the naive `:8080` form
 *   produces `https://<name>-5173.app.github.dev:8080`, which resolves to
 *   nothing. DL3 says the project must run in Codespaces, and with the naive
 *   form the console could not reach its own API there at all.
 * - **Deployed** — different host entirely, so `VITE_API_BASE_URL` wins and
 *   none of this runs.
 *
 * Exported for its own test: the Codespaces case is not reachable from a
 * laptop, so the only honest way to check it is to feed the function the URL
 * Codespaces actually serves.
 */
export function deriveApiBaseUrl(location: {
  protocol: string;
  hostname: string;
}): string {
  // A forwarded port lives in the first label of the hostname. Rewriting that
  // label is the only thing that works, and appending a port is the thing that
  // silently does not.
  if (location.hostname.endsWith(".app.github.dev")) {
    return `${location.protocol}//${location.hostname.replace(
      new RegExp(`-${SPA_PORT}\\.`),
      `-${API_PORT}.`,
    )}`;
  }
  return `${location.protocol}//${location.hostname}:${API_PORT}`;
}

export const API_BASE_URL: string =
  import.meta.env.VITE_API_BASE_URL?.trim() || deriveApiBaseUrl(window.location);

const KEY_STORAGE = "usss.api_key";
const IDENTITY_STORAGE = "usss.identity";

/** Thrown before any request when there is no key. The app renders sign-in. */
export class NotSignedIn extends Error {
  constructor() {
    super("not signed in");
  }
}

export class ApiError extends Error {
  // Plain fields rather than constructor parameter properties: the app builds
  // with `erasableSyntaxOnly`, which rejects any TypeScript that emits runtime
  // code.
  status: number;
  code: string;
  classification: string;
  /** Present on a refusal the customer can act on. Never built by this client. */
  actionUrl?: string;

  constructor(
    status: number,
    code: string,
    classification: string,
    message: string,
    actionUrl?: string,
  ) {
    super(message);
    this.status = status;
    this.code = code;
    this.classification = classification;
    this.actionUrl = actionUrl;
  }
}

export function storedKey(): string | null {
  return sessionStorage.getItem(KEY_STORAGE);
}

export function storedIdentity(): string | null {
  return sessionStorage.getItem(IDENTITY_STORAGE);
}

export function clearKey(): void {
  sessionStorage.removeItem(KEY_STORAGE);
  sessionStorage.removeItem(IDENTITY_STORAGE);
}

export interface DevLoginResponse {
  key: string;
  key_id: string;
  prefix_display: string;
  user_id: number;
}

/**
 * The PoC sign-in the brief allows, and the whole of it.
 *
 * `email` identifies the account: an address nobody has used yet **is** a
 * brand-new user, which is how the state a reviewer meets first — every
 * provider unconnected — is reachable without touching the database.
 */
export async function devLogin(email: string): Promise<DevLoginResponse> {
  const response = await fetch(`${API_BASE_URL}/v1/auth/dev-login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, name: "console" }),
  });
  if (!response.ok) {
    throw new Error(`sign-in failed: ${response.status} ${response.statusText}`);
  }
  const body = (await response.json()) as DevLoginResponse;
  sessionStorage.setItem(KEY_STORAGE, body.key);
  sessionStorage.setItem(IDENTITY_STORAGE, email);
  return body;
}

/** Adopt a key minted elsewhere (`make smoke`, `curl`) without re-signing-in. */
export async function adoptKey(key: string, label: string): Promise<void> {
  const response = await fetch(`${API_BASE_URL}/v1/connections`, {
    headers: { "X-API-Key": key },
  });
  if (!response.ok) throw new Error(`that key was not accepted (${response.status})`);
  sessionStorage.setItem(KEY_STORAGE, key);
  sessionStorage.setItem(IDENTITY_STORAGE, label);
}

export interface RequestOptions {
  method?: string;
  body?: unknown;
  signal?: AbortSignal;
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
  const key = storedKey();
  if (!key) throw new NotSignedIn();

  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: options.method ?? "GET",
    headers: {
      "X-API-Key": key,
      ...(options.body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    signal: options.signal,
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
      // `action_url` is the general name; `reconnect_url` is the older one kept
      // for the revoked-grant case. Read both, prefer the general one, and
      // never build either.
      error?.action_url ?? error?.reconnect_url,
    );
  }

  return { data: payload as T, headers: response.headers };
}

/**
 * Open the progress stream.
 *
 * `fetch` + `ReadableStream` rather than `EventSource`, which cannot set
 * headers at all and would force the key into the query string — the reason
 * this is so often built the insecure way. Returns a cleanup function.
 *
 * The stream is an **accelerator**. It carries nothing the snapshot lacks, so
 * every failure path here is "stop, and let polling carry it" rather than
 * anything the customer should ever see.
 */
export function streamSearchEvents(
  searchId: string,
  onEvent: (event: string) => void,
): () => void {
  const key = storedKey();
  const controller = new AbortController();
  if (!key) return () => controller.abort();

  void (async () => {
    try {
      const response = await fetch(`${API_BASE_URL}/v1/searches/${searchId}/events`, {
        headers: { "X-API-Key": key },
        signal: controller.signal,
      });
      if (!response.ok || !response.body) return;

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) return;
        buffer += decoder.decode(value, { stream: true });
        // SSE frames are separated by a blank line. Whatever follows the last
        // separator is a partial frame and stays in the buffer.
        const frames = buffer.split("\n\n");
        buffer = frames.pop() ?? "";
        for (const frame of frames) {
          const name = frame
            .split("\n")
            .find((line) => line.startsWith("event:"))
            ?.slice(6)
            .trim();
          if (name) onEvent(name);
        }
      }
    } catch {
      // Aborted, buffered by a proxy, or the connection died. Polling is the
      // primary path and is still running, so this is silent by design.
    }
  })();

  return () => controller.abort();
}
