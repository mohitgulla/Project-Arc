import { useSyncExternalStore } from "react";

/** TOWER_DESIGN §6 breakpoints: mobile <=768, tablet 769-1279, desktop >=1280. */
export type Layout = "mobile" | "tablet" | "desktop";

export const QUERIES = {
  mobile: "(max-width: 768px)",
  desktop: "(min-width: 1280px)",
} as const;

function subscribe(cb: () => void): () => void {
  const mqs = Object.values(QUERIES).map((q) => window.matchMedia(q));
  mqs.forEach((m) => m.addEventListener("change", cb));
  return () => mqs.forEach((m) => m.removeEventListener("change", cb));
}

function snapshot(): Layout {
  if (window.matchMedia(QUERIES.mobile).matches) return "mobile";
  if (window.matchMedia(QUERIES.desktop).matches) return "desktop";
  return "tablet";
}

export function useLayout(): Layout {
  return useSyncExternalStore(subscribe, snapshot, () => "desktop");
}
