/**
 * TanStack Query hooks. The server owns the state; the client caches it.
 *
 * No Redux, no Zustand, and the reason is the send gate rather than taste: a
 * store invites someone to derive `canSend` locally, duplicating a rule that
 * belongs to the API and that the whole product exists to enforce in one place.
 * The only module-level state in the app is the API key in `sessionStorage`.
 *
 * Nothing in this file decides anything. Every hook is a transport: it names a
 * route, hands back what came off it, and says when to ask again.
 */

import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type UseInfiniteQueryResult,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";
import { useEffect } from "react";
import { api, apiWithHeaders, streamSearchEvents } from "./client";
import type {
  AuthorizeResponse,
  ConnectionsResponse,
  DraftEnvelope,
  SearchListRow,
  SearchSnapshot,
  SendView,
} from "./types";

/**
 * While any source is non-terminal, poll every second (task 11.3).
 *
 * This is the **primary** progress path, not a fallback. The stream below only
 * makes the same facts arrive sooner.
 */
const SEARCH_POLL_MS = 1000;

/** A send in flight changes underneath the list without anyone touching it. */
const SEND_POLL_MS = 2000;

const PAGE_SIZE = 25;

// -------------------------------------------------------------- connections

export function useConnections(): UseQueryResult<ConnectionsResponse> {
  return useQuery({
    queryKey: ["connections"],
    queryFn: () => api<ConnectionsResponse>("/v1/connections"),
    // A grant completes in another tab or another window. Coming back to a
    // stale "not connected" after authorizing reads as the connect having
    // failed, which is the opposite of what happened.
    refetchOnWindowFocus: true,
  });
}

/**
 * Follow an `action_url` and get back where the browser should go.
 *
 * 🔴 **`action_url` is an API route, not a browser destination.** It answers
 * `{authorize_url}` and it needs the `X-API-Key` header, so rendering it as an
 * `<a href>` produces either a 401 or — because the SPA and the API are
 * different origins — a dead link inside the SPA's own router. That is how this
 * link has now been broken on four surfaces across three phases. It is fetched
 * here, once, and every surface that offers the action uses this.
 */
export function useAuthorize(): UseMutationResult<AuthorizeResponse, Error, string> {
  return useMutation({
    mutationFn: (actionUrl: string) => api<AuthorizeResponse>(actionUrl),
  });
}

export function useDisconnect(): UseMutationResult<unknown, Error, number> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (connectionId: number) =>
      api(`/v1/connections/${connectionId}`, { method: "DELETE" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["connections"] });
    },
  });
}

// ------------------------------------------------------------------- search

export function useSearch(searchId: string | undefined): UseQueryResult<SearchSnapshot> {
  return useQuery({
    queryKey: ["search", searchId],
    enabled: Boolean(searchId),
    queryFn: () => api<SearchSnapshot>(`/v1/searches/${searchId}`),
    // Polling stops the moment the search is finished — `finished` means "no
    // source will change again", including the ones that failed.
    refetchInterval: (query) =>
      query.state.data && !query.state.data.finished ? SEARCH_POLL_MS : false,
    // Keep polling when the tab is not focused. TanStack Query pauses intervals
    // in the background by default, which is right for a dashboard and wrong
    // here: a customer who switches tabs while a slow source is still running
    // comes back to a page frozen mid-search with no way to tell that apart
    // from a hang.
    refetchIntervalInBackground: true,
  });
}

/**
 * The SSE accelerator (task 11.3).
 *
 * It carries no information the snapshot lacks, so it does exactly one thing:
 * invalidate the snapshot query sooner than the next poll would have. There is
 * no fallback path to write, because the fallback is already running — polling
 * is never conditional on the stream. A stream that is buffered by a proxy,
 * refused, or silent for a minute costs immediacy and nothing else, which is
 * the property `risks.md` R8 asks for.
 */
export function useSearchStream(searchId: string | undefined, enabled: boolean): void {
  const queryClient = useQueryClient();
  useEffect(() => {
    if (!searchId || !enabled) return;
    return streamSearchEvents(searchId, () => {
      void queryClient.invalidateQueries({ queryKey: ["search", searchId] });
    });
  }, [searchId, enabled, queryClient]);
}

export function useCreateSearch(): UseMutationResult<{ search_id: string }, Error, string> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (query: string) =>
      api<{ search_id: string }>("/v1/searches", { method: "POST", body: { query } }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["searches"] });
    },
  });
}

/** Run it again as a **new** search. Never in place — the old one is evidence. */
export function useRerunSearch(): UseMutationResult<{ search_id: string }, Error, string> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (searchId: string) =>
      api<{ search_id: string }>(`/v1/searches/${searchId}/rerun`, { method: "POST" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["searches"] });
    },
  });
}

// ------------------------------------------------------------------- drafts

export function useDraft(draftId: string | undefined): UseQueryResult<DraftEnvelope> {
  return useQuery({
    queryKey: ["draft", draftId],
    enabled: Boolean(draftId),
    // Never cached across a reload: the customer must always confirm something
    // they can currently see, so the confirm screen re-fetches rather than
    // rendering a digest it kept.
    staleTime: 0,
    gcTime: 0,
    queryFn: () => api<DraftEnvelope>(`/v1/drafts/${draftId}`),
  });
}

export function useCreateDraft(): UseMutationResult<
  DraftEnvelope,
  Error,
  {
    channel: "gmail" | "slack";
    to: string;
    body: string;
    subject?: string;
    /**
     * Which grant to send from. Omitted, the API picks the first active one —
     * fine with a single account, and a silent coin-toss with two. Passed, so
     * the customer's choice is the one that ships.
     */
    connection_id?: number;
  }
> {
  return useMutation({
    mutationFn: (input) => api<DraftEnvelope>("/v1/drafts", { method: "POST", body: input }),
  });
}

export interface SendResult {
  send: SendView;
  /** Straight off the `Idempotent-Replayed` header — the API's word, not ours. */
  replayed: boolean;
}

export function useSendDraft(): UseMutationResult<
  SendResult,
  Error,
  { draftId: string; confirmedSha256: string }
> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({ draftId, confirmedSha256 }) => {
      const response = await apiWithHeaders<SendView>(`/v1/drafts/${draftId}/send`, {
        method: "POST",
        body: { confirmed_sha256: confirmedSha256 },
      });
      return {
        send: response.data,
        replayed: response.headers.get("Idempotent-Replayed") === "true",
      };
    },
    onSuccess: (result) => {
      void queryClient.invalidateQueries({ queryKey: ["sends"] });
      void queryClient.invalidateQueries({ queryKey: ["send", result.send.send_id] });
    },
  });
}

// ------------------------------------------------------------------ history

interface SendsPage {
  sends: SendView[];
  next_cursor: string | null;
}

export function useSends(includeSeed: boolean): UseInfiniteQueryResult<SendView[], Error> {
  return useInfiniteQuery({
    queryKey: ["sends", includeSeed],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) =>
      api<SendsPage>(
        `/v1/sends?limit=${PAGE_SIZE}&include_seed=${includeSeed}` +
          (pageParam ? `&cursor=${encodeURIComponent(pageParam)}` : ""),
      ),
    // `next_cursor: null` means there is no more — a fact, not a guess: the API
    // reads one row past the limit to know it (`api-design.md` §Conventions).
    getNextPageParam: (last) => last.next_cursor,
    select: (data) => data.pages.flatMap((page) => page.sends),
    // Poll only while something can still change underneath the list. A send in
    // flight moves without anyone touching it; a page of settled rows does not,
    // and a timer that never stops is load the product does not need — locally
    // it competes with the inline worker for a five-connection pool, which
    // surfaces as unrelated requests timing out rather than as a slow list.
    refetchInterval: (query) =>
      (query.state.data?.pages ?? []).some((page) =>
        page.sends.some((send) => send.state === "in_flight"),
      )
        ? SEND_POLL_MS
        : false,
    // Unlike a running search, a history list nobody is looking at does not
    // need to stay warm — and locally this timer runs against the same
    // five-connection pool the inline worker is using, so a console left open
    // in a background tab measurably slows everything else down.
    refetchIntervalInBackground: false,
  });
}

interface SearchesPage {
  searches: SearchListRow[];
  next_cursor: string | null;
}

export function useSearchHistory(
  includeSeed: boolean,
): UseInfiniteQueryResult<SearchListRow[], Error> {
  return useInfiniteQuery({
    queryKey: ["searches", includeSeed],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) =>
      api<SearchesPage>(
        `/v1/searches?limit=${PAGE_SIZE}&include_seed=${includeSeed}` +
          (pageParam ? `&cursor=${encodeURIComponent(pageParam)}` : ""),
      ),
    getNextPageParam: (last) => last.next_cursor,
    select: (data) => data.pages.flatMap((page) => page.searches),
  });
}

export function useSend(sendId: string | undefined): UseQueryResult<SendView> {
  return useQuery({
    queryKey: ["send", sendId],
    enabled: Boolean(sendId),
    queryFn: () => api<SendView>(`/v1/sends/${sendId}`),
    refetchInterval: (query) => (query.state.data?.state === "in_flight" ? 1000 : false),
    refetchIntervalInBackground: true,
  });
}

export function useRetrySend(): UseMutationResult<SendView, Error, string> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (sendId: string) =>
      api<SendView>(`/v1/sends/${sendId}/retry`, { method: "POST" }),
    onSuccess: (_data, sendId) => {
      void queryClient.invalidateQueries({ queryKey: ["send", sendId] });
      void queryClient.invalidateQueries({ queryKey: ["sends"] });
    },
  });
}

/**
 * Settle an in-doubt send. **The one action `uncertain` actually needs.**
 *
 * Never a retry: re-sending a message that may already have arrived is the
 * misfire this product exists to prevent. Which resolutions are on offer comes
 * off `uncertainty.resolutions`, so the console never invents one.
 */
export function useResolveSend(): UseMutationResult<
  SendView,
  Error,
  { sendId: string; resolution: string; note?: string }
> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ sendId, resolution, note }) =>
      api<SendView>(`/v1/sends/${sendId}/resolve`, {
        method: "POST",
        body: { resolution, note: note || null },
      }),
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({ queryKey: ["send", variables.sendId] });
      void queryClient.invalidateQueries({ queryKey: ["sends"] });
    },
  });
}
