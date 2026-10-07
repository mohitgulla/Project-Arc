// E12.6 (D51): pure helpers for the Universe page (/ops/universe). The page shows the
// stored resolve from `/api/ops/universe`; nothing here re-derives the dedupe or the cap.
import type { components } from "./api.gen";

type Schemas = components["schemas"];
export type Universe = Schemas["UniverseResponse"];
export type UniverseActive = Schemas["UniverseActiveRow"];
export type UniverseTier = Schemas["UniverseTierRow"];
export type UniverseDropped = Schemas["UniverseDroppedRow"];

/** Tier precedence (core > momentum > trending > discovery), the page's section order. */
export const TIER_ORDER = ["core", "momentum", "trending", "discovery"] as const;

/** `core` -> `Core` (Title Case labels, D48). */
export function tierLabel(name: string): string {
  return name ? name[0]!.toUpperCase() + name.slice(1) : "—";
}

/** `Active 50/50 · Core 25 · Momentum 17 · Trending 0 · Discovery 8` (pinned header). */
export function summaryLine(u: Pick<Universe, "active" | "active_max" | "tiers">): string {
  const parts = [`Active ${u.active.length}/${u.active_max}`];
  for (const t of sortTiers(u.tiers)) parts.push(`${tierLabel(t.name)} ${t.active}`);
  return parts.join(" · ");
}

export function sortTiers<T extends { name: string }>(tiers: T[]): T[] {
  const idx = (n: string) => {
    const i = (TIER_ORDER as readonly string[]).indexOf(n);
    return i < 0 ? TIER_ORDER.length : i;
  };
  return [...tiers].sort((a, b) => idx(a.name) - idx(b.name));
}

/** Active members of one tier, by rank (the stored order within a tier). */
export function membersOf(active: UniverseActive[], tier: string): UniverseActive[] {
  return active.filter((m) => m.tier === tier).sort((a, b) => a.rank - b.rank);
}

/** Footer: which tiers refresh on their own jobs (D56: no trending; discovery = Scout). */
export function refreshLine(model: string | null | undefined): string {
  return model === "d56"
    ? "momentum refreshes on its own job; discovery is written by the Scout."
    : "momentum and trending refresh on their own jobs.";
}

/** Plain words for a drop reason. */
export const DROP_LABEL: Record<string, string> = {
  over_active_cap: "past the active-list cap",
  over_tier_size: "past the tier's size",
};

export function dropLabel(reason: string): string {
  return DROP_LABEL[reason] ?? reason.replace(/_/g, " ");
}

/** A chip's detail lines: source, reason, `also in Momentum, Trending`. */
export function memberDetail(m: UniverseActive): string[] {
  const out = [`#${m.rank} in ${tierLabel(m.tier)} · source ${m.source || "—"}`];
  if (m.reason) out.push(m.reason);
  const also = m.also_in ?? [];
  if (also.length) out.push(`also in ${also.map(tierLabel).join(", ")}`);
  return out;
}

/** Tier header caption: `offered 24 · active 17 / 25`. */
export function tierCounts(t: UniverseTier): string {
  const cap = t.size_cap == null ? "" : ` / ${t.size_cap}`;
  return `offered ${t.offered} · active ${t.active}${cap}`;
}

/** Freshness state of the stored resolve. */
export function resolveLabel(u: Pick<Universe, "state" | "resolved_for">): string {
  if (u.state === "today") return "Resolved Today";
  if (u.state === "stale") return `Not Resolved Today${u.resolved_for ? ` · last ${u.resolved_for}` : ""}`;
  return "Never Resolved";
}

/** `SPY QQQ (regime only, not traded)`. */
export function marketReferenceLine(symbols: string[]): string {
  return symbols.length ? `${symbols.join(" ")} (regime only, not traded)` : "none";
}

/** Config page label for the `universe` key (D51: the core tier, ≤ 30). */
export const CORE_KEY_LABEL = "Core Universe (≤ 30)";
export const CORE_MAX = 30;

/** The config page's inline warning when the effective `universe` override is ignored
 *  (more than 30 distinct names; the same rule as `arc.universe.tiers.core_tickers`). */
export function coreOverrideWarning(value: unknown): string | null {
  const n = Array.isArray(value) ? new Set(value.map((x) => String(x).trim().replace(/^\$/, "").toUpperCase()).filter(Boolean)).size : 0;
  return n > CORE_MAX
    ? `${n} names > ${CORE_MAX}: a pre-D51 flat list, so the resolver uses config/universe.yaml core instead.`
    : null;
}

/** E13.14 (D56): today's Scout discovery fill, `Discovery fill 8 / 20 today` (null under d51). */
export function discoveryFillLine(u: Pick<Universe, "discovery_fill" | "tiers">): string | null {
  if (u.discovery_fill == null) return null;
  const cap = u.tiers.find((t) => t.name === "discovery")?.size_cap;
  return `Discovery fill ${u.discovery_fill}${cap == null ? "" : ` / ${cap}`} today (Scout)`;
}

/** E13.14: a tail cut, `#18 in Discovery` (rank when stored). */
export function tailCutDetail(d: UniverseDropped): string {
  return d.rank == null ? tierLabel(d.tier) : `#${d.rank} in ${tierLabel(d.tier)}`;
}
