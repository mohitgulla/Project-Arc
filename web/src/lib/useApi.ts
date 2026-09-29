import { keepPreviousData, useQuery } from "@tanstack/react-query";

import { apiGet, type Meta, type Positions, type Snapshot } from "./api";
import type { Overview, OverviewRange } from "./overview";
import { useSettings } from "./settings";

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

export function usePositions(status: "open" | "closed" | "all") {
  const poll = usePoll();
  return useQuery<Positions>({
    queryKey: ["positions", status],
    queryFn: ({ signal }) => apiGet("/api/positions", { query: { status }, signal }),
    placeholderData: keepPreviousData,
    ...poll,
  });
}
