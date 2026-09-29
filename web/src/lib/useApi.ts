import { useQuery } from "@tanstack/react-query";

import { apiGet, type Meta, type Snapshot } from "./api";
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
