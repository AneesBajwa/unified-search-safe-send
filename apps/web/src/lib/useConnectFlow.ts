/**
 * One connect flow, used by every surface that offers one.
 *
 * There are three places a customer can be offered a grant — the connections
 * page, a source chip on a search, and a refusal from the send gate — and phase
 * 3 and phase 4 each found this link broken on a *different* one of them,
 * because each surface built it independently. So it is built once, here.
 *
 * The shape of it:
 *
 * 1. Take `action_url` off the payload. Never construct it.
 * 2. `GET` it **with the API key header** — it is an API route, not a browser
 *    destination, and it answers `{authorize_url}`.
 * 3. Send the browser to `authorize_url`, in a popup where one is allowed.
 * 4. Watch `GET /connections` until it changes, then close the popup.
 *
 * Step 4 exists because the OAuth callback is a JSON API route: the provider
 * redirects the browser to the API, which answers `{"connection": …}`. In a
 * popup that page is never seen — the window closes on the fact of the
 * connection landing. In the same tab the customer would end up looking at raw
 * JSON with no way back, which is why the popup is the primary path and the
 * same-tab navigation is only the fallback for a blocked one.
 */

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api/client";
import { useAuthorize } from "../api/hooks";
import type { ConnectionsResponse } from "../api/types";

/** How long to keep watching before giving up on a window nobody finished. */
const WATCH_TIMEOUT_MS = 180_000;
const WATCH_INTERVAL_MS = 1500;

export type ConnectPhase = "idle" | "authorizing" | "waiting" | "connected" | "failed";

export interface ConnectFlow {
  phase: ConnectPhase;
  /** The provider's own words about why we are asking, shown before we ask. */
  error: string | null;
  /** True when the popup was blocked and the current tab had to be used. */
  usedSameTab: boolean;
  start: (actionUrl: string) => void;
  reset: () => void;
}

export function useConnectFlow(): ConnectFlow {
  const authorize = useAuthorize();
  const queryClient = useQueryClient();
  const [phase, setPhase] = useState<ConnectPhase>("idle");
  const [error, setError] = useState<string | null>(null);
  const [usedSameTab, setUsedSameTab] = useState(false);
  const timers = useRef<number[]>([]);

  const clearTimers = useCallback(() => {
    for (const id of timers.current) window.clearInterval(id);
    timers.current = [];
  }, []);

  useEffect(() => clearTimers, [clearTimers]);

  const start = useCallback(
    (actionUrl: string) => {
      setError(null);
      setUsedSameTab(false);
      setPhase("authorizing");

      // Opened synchronously inside the click, before any await: a popup opened
      // after a network round trip is blocked by every browser, and the failure
      // reads as the button doing nothing at all.
      const popup = window.open("", "usss-connect", "width=520,height=680");

      void (async () => {
        // What "connected" is measured against. Read before the grant, so
        // "something changed" needs no knowledge of which provider or which id
        // was being repaired.
        let before = "";
        try {
          before = JSON.stringify(await api<ConnectionsResponse>("/v1/connections"));
        } catch {
          // A snapshot we could not take just means the watcher below relies on
          // the window closing instead. Not worth failing the flow over.
        }

        let authorizeUrl: string;
        try {
          const grant = await authorize.mutateAsync(actionUrl);
          authorizeUrl = grant.authorize_url;
        } catch (exc) {
          popup?.close();
          setPhase("failed");
          setError(exc instanceof Error ? exc.message : String(exc));
          return;
        }

        if (popup) {
          popup.location.href = authorizeUrl;
        } else {
          // Blocked. The grant still works; the customer just lands on the
          // callback's JSON and has to come back. Said out loud in the UI
          // rather than left as a surprise.
          setUsedSameTab(true);
          window.location.href = authorizeUrl;
          return;
        }

        setPhase("waiting");
        const startedAt = Date.now();
        const watcher = window.setInterval(() => {
          void (async () => {
            const expired = Date.now() - startedAt > WATCH_TIMEOUT_MS;
            let changed = false;
            try {
              const now = JSON.stringify(await api<ConnectionsResponse>("/v1/connections"));
              changed = before !== "" && now !== before;
            } catch {
              // Ignore: the next tick asks again.
            }

            if (changed) {
              popup.close();
              window.clearInterval(watcher);
              await queryClient.invalidateQueries({ queryKey: ["connections"] });
              setPhase("connected");
              return;
            }
            if (popup.closed || expired) {
              window.clearInterval(watcher);
              await queryClient.invalidateQueries({ queryKey: ["connections"] });
              // Closed without anything changing is not an error — a customer is
              // allowed to change their mind at the consent screen.
              setPhase("idle");
            }
          })();
        }, WATCH_INTERVAL_MS);
        timers.current.push(watcher);
      })();
    },
    [authorize, queryClient],
  );

  const reset = useCallback(() => {
    clearTimers();
    setPhase("idle");
    setError(null);
  }, [clearTimers]);

  return { phase, error, usedSameTab, start, reset };
}
