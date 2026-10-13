import { keepPreviousData, useQuery } from "@tanstack/react-query";

import { apiGet, type ApiPath, type Meta, type Positions, type Snapshot } from "./api";
import type { ExperimentDetail, ExperimentsList } from "./experiments";
import type { ContextEntry, RunDetail } from "./ops";
import type { Overview, OverviewRange } from "./overview";
import type { Funnel, Performance } from "./performance";
import { useArm } from "./armContext";
import { withArm } from "./arm";
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
  const arm = useArm();
  const poll = usePoll();
  return useQuery<Meta>({
    queryKey: [arm, "meta"],
    queryFn: ({ signal }) => apiGet("/api/meta", { query: withArm(undefined, arm), signal }),
    ...poll,
  });
}

/**
 * E15.7: Arc Investor's `/api/meta` (no `?arm`), for the arm switcher's list. `arms` always
 * come from control, and this still loads when the URL names an arm the API refuses (404).
 * Shares the cache entry with `useMeta()` on Arc Investor.
 */
export function useControlMeta() {
  const poll = usePoll();
  return useQuery<Meta>({
    queryKey: [null, "meta"],
    queryFn: ({ signal }) => apiGet("/api/meta", { signal }),
    ...poll,
  });
}

export function useSnapshot() {
  const arm = useArm();
  const poll = usePoll();
  return useQuery<Snapshot>({
    queryKey: [arm, "snapshot"],
    queryFn: ({ signal }) => apiGet("/api/snapshot", { query: withArm(undefined, arm), signal }),
    ...poll,
  });
}

export function useOverview(range: OverviewRange) {
  const arm = useArm();
  const poll = usePoll();
  return useQuery<Overview>({
    queryKey: [arm, "overview", range],
    queryFn: ({ signal }) => apiGet("/api/overview", { query: withArm({ range }, arm), signal }),
    placeholderData: keepPreviousData,
    ...poll,
  });
}

export function useTrades(q: TradeQuery) {
  const arm = useArm();
  const poll = usePoll();
  return useQuery<TradeList>({
    queryKey: [arm, "trades", apiQuery(q)],
    queryFn: ({ signal }) => apiGet("/api/trades", { query: withArm(apiQuery(q), arm), signal }),
    placeholderData: keepPreviousData,
    ...poll,
  });
}

export function useTradeFilters() {
  const arm = useArm();
  return useQuery<TradeFilterOptions>({
    queryKey: [arm, "trade-filters"],
    queryFn: ({ signal }) => apiGet("/api/trades/filters", { query: withArm(undefined, arm), signal }),
    staleTime: 60_000,
  });
}

export function useTrade(hash: string | undefined) {
  const arm = useArm();
  const poll = usePoll();
  return useQuery<TradeDetail>({
    queryKey: [arm, "trade", hash],
    // The path template is typed; the hash is URL-safe hex.
    queryFn: ({ signal }) =>
      apiGet(`/api/trades/${hash}` as "/api/trades/{proposal_hash}", {
        query: withArm(undefined, arm),
        signal,
      }),
    enabled: Boolean(hash),
    ...poll,
  });
}

export function useSearch(q: string) {
  const arm = useArm();
  return useQuery<SearchResponse>({
    queryKey: [arm, "search", q],
    queryFn: ({ signal }) => apiGet("/api/search", { query: withArm({ q }, arm), signal }),
    enabled: q.trim().length > 0,
    staleTime: 30_000,
    placeholderData: keepPreviousData,
  });
}

export function usePositions(status: "open" | "closed" | "all") {
  const arm = useArm();
  const poll = usePoll();
  return useQuery<Positions>({
    queryKey: [arm, "positions", status],
    queryFn: ({ signal }) => apiGet("/api/positions", { query: withArm({ status }, arm), signal }),
    placeholderData: keepPreviousData,
    ...poll,
  });
}

/** Performance (E8.7c): the server caches each response for 60 s per DB state. */
/** E13.14 (D56): the idea funnel for ET days [from, to] (the Performance page's period). */
export function useFunnel(period: { first: string; last: string } | undefined) {
  const arm = useArm();
  const poll = usePoll();
  return useQuery<Funnel>({
    queryKey: [arm, "funnel", period?.first, period?.last],
    queryFn: ({ signal }) =>
      apiGet("/api/performance/funnel", { query: withArm({ from: period?.first, to: period?.last }, arm), signal }),
    enabled: !!period,
    placeholderData: keepPreviousData,
    ...poll,
  });
}

export function usePerformance(q: Record<string, string>) {
  const arm = useArm();
  const poll = usePoll();
  return useQuery<Performance>({
    queryKey: [arm, "performance", q],
    queryFn: ({ signal }) => apiGet("/api/performance", { query: withArm(q, arm), signal }),
    placeholderData: keepPreviousData,
    ...poll,
  });
}

type OpsPath = Extract<
  ApiPath,
  | "/api/ops/session"
  | "/api/ops/health"
  | "/api/ops/alerts"
  | "/api/ops/halts"
  | "/api/ops/runs"
  | "/api/ops/budget"
  | "/api/ops/context"
  | "/api/ops/sources"
  | "/api/ops/llm"
  | "/api/ops/config"
  | "/api/ops/universe"
>;

/** Ops & pipeline (E8.7d): one polled query per card. */
export function useOps<P extends OpsPath>(path: P, query?: Record<string, string | number>) {
  const arm = useArm();
  const poll = usePoll();
  return useQuery({
    queryKey: [arm, "ops", path, query ?? {}],
    queryFn: ({ signal }) => apiGet(path, { query: withArm(query, arm), signal }),
    placeholderData: keepPreviousData,
    ...poll,
  });
}

export function useRun(runId: string | undefined) {
  const arm = useArm();
  const poll = usePoll();
  return useQuery<RunDetail>({
    queryKey: [arm, "ops-run", runId],
    queryFn: ({ signal }) =>
      apiGet(`/api/ops/runs/${runId}` as "/api/ops/runs/{run_id}", { query: withArm(undefined, arm), signal }),
    enabled: Boolean(runId),
    ...poll,
  });
}

/** Experiments (E10.5): the latest stored evaluation per experiment. */
export function useExperiments() {
  const arm = useArm();
  const poll = usePoll();
  return useQuery<ExperimentsList>({
    queryKey: [arm, "experiments"],
    queryFn: ({ signal }) => apiGet("/api/experiments", { query: withArm(undefined, arm), signal }),
    placeholderData: keepPreviousData,
    ...poll,
  });
}

export function useExperiment(id: string | undefined) {
  const arm = useArm();
  const poll = usePoll();
  return useQuery<ExperimentDetail>({
    queryKey: [arm, "experiment", id],
    queryFn: ({ signal }) =>
      apiGet(`/api/experiments/${id}` as "/api/experiments/{experiment_id}", { query: withArm(undefined, arm), signal }),
    enabled: Boolean(id),
    ...poll,
  });
}

export function useContextEntry(id: string | undefined) {
  const arm = useArm();
  return useQuery<ContextEntry>({
    queryKey: [arm, "ops-context", id],
    queryFn: ({ signal }) =>
      apiGet(`/api/ops/context/${id}` as "/api/ops/context/{entry_id}", { query: withArm(undefined, arm), signal }),
    enabled: Boolean(id),
    staleTime: 60_000,
  });
}
