import { keepPreviousData, useQuery } from "@tanstack/react-query";

import { apiGet, type Meta, type Positions, type Snapshot } from "./api";
import type { Overview, OverviewRange } from "./overview";
import type { Performance } from "./performance";
import { useSettings } from "./settings";
import {
  apiQuery,
  type SearchResponse,
  type TradeDetail,
  type TradeFilterOptions,
  type TradeList,
  type TradeQuery,
} from "./trades";

/**
 * Polling queries (TOWER_DESIGN §7): refetch at the Settings interval (30/60/120 s),
 * paused while the tab is hidden (refetchIntervalInBackground=false) and refreshed on
 * focus. No websockets.
 */
function usePoll() {
  const { refreshS } = useSettings();
  return {
    refetchInterval: refreshS * 1000,
    refetchIntervalInBackground: false,
    refetchOnWindowFocus: true,
  } as const;
}

export function useMeta() {
  const poll = usePoll();
  return useQuery<Meta>({
    queryKey: ["meta"],
    queryFn: ({ signal }) => apiGet("/api/meta", { signal }),
    ...poll,
  });
}

export function useSnapshot() {
  const poll = usePoll();
  return useQuery<Snapshot>({
    queryKey: ["snapshot"],
    queryFn: ({ signal }) => apiGet("/api/snapshot", { signal }),
    ...poll,
  });
}

export function useOverview(range: OverviewRange) {
  const poll = usePoll();
  return useQuery<Overview>({
    queryKey: ["overview", range],
    queryFn: ({ signal }) => apiGet("/api/overview", { query: { range }, signal }),
    placeholderData: keepPreviousData,
    ...poll,
  });
}

export function useTrades(q: TradeQuery) {
  const poll = usePoll();
  return useQuery<TradeList>({
    queryKey: ["trades", apiQuery(q)],
    queryFn: ({ signal }) => apiGet("/api/trades", { query: apiQuery(q), signal }),
    placeholderData: keepPreviousData,
    ...poll,
  });
}

export function useTradeFilters() {
  return useQuery<TradeFilterOptions>({
    queryKey: ["trade-filters"],
    queryFn: ({ signal }) => apiGet("/api/trades/filters", { signal }),
    staleTime: 60_000,
  });
}

export function useTrade(hash: string | undefined) {
  const poll = usePoll();
  return useQuery<TradeDetail>({
    queryKey: ["trade", hash],
    // The path template is typed; the hash is URL-safe hex.
    queryFn: ({ signal }) =>
      apiGet(`/api/trades/${hash}` as "/api/trades/{proposal_hash}", {
        signal,
      }),
    enabled: Boolean(hash),
    ...poll,
  });
}

export function useSearch(q: string) {
  return useQuery<SearchResponse>({
    queryKey: ["search", q],
    queryFn: ({ signal }) => apiGet("/api/search", { query: { q }, signal }),
    enabled: q.trim().length > 0,
    staleTime: 30_000,
    placeholderData: keepPreviousData,
  });
}

export function usePositions(status: "open" | "closed" | "all") {
  const poll = usePoll();
  return useQuery<Positions>({
    queryKey: ["positions", status],
    queryFn: ({ signal }) => apiGet("/api/positions", { query: { status }, signal }),
    placeholderData: keepPreviousData,
    ...poll,
  });
}

/** Performance (E8.7c): the server caches each response for 60 s per DB state. */
export function usePerformance(q: Record<string, string>) {
  const poll = usePoll();
  return useQuery<Performance>({
    queryKey: ["performance", q],
    queryFn: ({ signal }) => apiGet("/api/performance", { query: q, signal }),
    placeholderData: keepPreviousData,
    ...poll,
  });
}
