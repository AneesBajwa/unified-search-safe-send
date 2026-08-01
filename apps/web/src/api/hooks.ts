/**
 * TanStack Query hooks. The server owns the state; the client caches it.
 *
 * No Redux, no Zustand, and the reason is the send gate rather than taste: a
 * store invites someone to derive `canSend` locally, duplicating a rule that
 * belongs to the API and that the whole product exists to enforce in one place.
 * The only module-level state in the app is the API key in `sessionStorage`.
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";
import { api, apiWithHeaders } from "./client";
import type {
  Confirmation,
  Connection,
  Draft,
  SearchSnapshot,
  SendView,
} from "./types";

/** While a search is unfinished, poll. The snapshot is the primary path. */
const POLL_MS = 800;

export function useConnections(): UseQueryResult<Connection[]> {
  return useQuery({
    queryKey: ["connections"],
    queryFn: async () => {
      const body = await api<{ connections: Connection[] }>("/v1/connections");
      return body.connections;
    },
  });
}

export function useSearch(searchId: string | undefined): UseQueryResult<SearchSnapshot> {
  return useQuery({
    queryKey: ["search", searchId],
    enabled: Boolean(searchId),
    queryFn: () => api<SearchSnapshot>(`/v1/searches/${searchId}`),
    // Polling stops the moment the search is finished — `finished` means "no
    // source will change again", including the ones that failed.
    refetchInterval: (query) =>
      query.state.data && !query.state.data.finished ? POLL_MS : false,
    // Keep polling when the tab is not focused. TanStack Query pauses intervals
    // in the background by default, which is right for a dashboard and wrong
    // here: a customer who switches tabs while a slow source is still running
    // comes back to a page frozen mid-search with no way to tell it apart from
    // a hang.
    refetchIntervalInBackground: true,
  });
}

export function useCreateSearch(): UseMutationResult<
  { search_id: string },
  Error,
  string
> {
  return useMutation({
    mutationFn: (query: string) =>
      api<{ search_id: string }>("/v1/searches", {
        method: "POST",
        body: { query },
      }),
  });
}

export interface DraftView {
  draft: Draft;
  confirmation: Confirmation;
}

export function useDraft(draftId: string | undefined): UseQueryResult<DraftView> {
  return useQuery({
    queryKey: ["draft", draftId],
    enabled: Boolean(draftId),
    // Never cached across a reload: the customer must always confirm something
    // they can currently see, so the confirm screen re-fetches rather than
    // rendering a digest it kept.
    staleTime: 0,
    queryFn: () => api<DraftView>(`/v1/drafts/${draftId}`),
  });
}

export function useCreateDraft(): UseMutationResult<
  DraftView,
  Error,
  { channel: "gmail" | "slack"; to: string; body: string; subject?: string }
> {
  return useMutation({
    mutationFn: (input) =>
      api<DraftView>("/v1/drafts", { method: "POST", body: input }),
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
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["sends"] });
    },
  });
}

export function useSends(): UseQueryResult<SendView[]> {
  return useQuery({
    queryKey: ["sends"],
    queryFn: async () => {
      const body = await api<{ sends: SendView[] }>("/v1/sends");
      return body.sends;
    },
    // A send in flight changes underneath the list, so history refreshes on a
    // slow tick rather than needing a manual reload.
    refetchInterval: 2000,
    refetchIntervalInBackground: true,
  });
}

export function useSend(sendId: string | undefined): UseQueryResult<SendView> {
  return useQuery({
    queryKey: ["send", sendId],
    enabled: Boolean(sendId),
    queryFn: () => api<SendView>(`/v1/sends/${sendId}`),
    refetchInterval: (query) =>
      query.state.data?.state === "in_flight" ? 1000 : false,
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

export interface SearchListRow {
  search_id: string;
  query: string;
  is_seed: boolean;
  created_at: string;
  finished: boolean;
  result_count: number;
}

export function useSearchHistory(): UseQueryResult<SearchListRow[]> {
  return useQuery({
    queryKey: ["searches"],
    queryFn: async () => {
      const body = await api<{ searches: SearchListRow[] }>("/v1/searches");
      return body.searches;
    },
  });
}
