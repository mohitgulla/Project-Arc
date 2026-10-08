// E12.6 (D56 since E13.15): pure helpers for the Universe page (/ops/universe). The page shows the
// stored resolve from `/api/ops/universe`; nothing here re-derives the dedupe or the cap.
import type { components } from "./api.gen";

type Schemas = components["schemas"];
export type Universe = Schemas["UniverseResponse"];
export type UniverseActive = Schemas["UniverseActiveRow"];
export type UniverseTier = Schemas["UniverseTierRow"];
export type UniverseDropped = Schemas["UniverseDroppedRow"];

/** Tier precedence (D58: core > momentum > discovery > trending), the page's section order. */
export const TIER_ORDER = ["core", "momentum", "discovery", "trending"] as const;

/** `core` -> `Core` (Title Case labels, D48). */
export function tierLabel(name: string): string {
  return name ? name[0]!.toUpperCase() + name.slice(1) : "—";
}

/** `Active 50/50 · Core 20 · Momentum 17 · Discovery 8` (pinned header). */
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

/** Footer: which tiers refresh on their own jobs (D56: discovery = Scout; D58: trending = retail buzz). */
export function refreshLine(): string {
  return "momentum refreshes on its own job; discovery is written by the Scout; trending is ranked daily from Reddit + Stocktwits.";
}

/** Plain words for a drop reason. */
export const DROP_LABEL: Record<string, string> = {
  over_active_cap: "past the active-list cap",
  over_tier_size: "past the tier's size",
};

export function dropLabel(reason: string): string {
  return DROP_LABEL[reason] ?? reason.replace(/_/g, " ");
}

/** A chip's detail lines: source, reason, `also in Momentum, Discovery`. */
export function memberDetail(m: UniverseActive): string[] {
  const out = [`#${m.rank} in ${tierLabel(m.tier)} · source ${m.source || "—"}`];
  if (m.inputs != null) out.push(m.inputs >= 2 ? "in both inputs" : "in one input");
  if (m.velocity_detail) out.push(`mention velocity: ${m.velocity_detail}`);
  if (m.sentiment) out.push(`Stocktwits: ${m.sentiment.replace(/^ST /, "")}`);
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

/** E13.14 (D56): today's Scout discovery fill, `Discovery fill 8 / 20 today` (null for a stored pre-cutover resolve). */
export function discoveryFillLine(u: Pick<Universe, "discovery_fill" | "tiers">): string | null {
  if (u.discovery_fill == null) return null;
  const cap = u.tiers.find((t) => t.name === "discovery")?.size_cap;
  return `Discovery fill ${u.discovery_fill}${cap == null ? "" : ` / ${cap}`} today (Scout)`;
}

/** E13.14: a tail cut, `#18 in Discovery` (rank when stored). */
export function tailCutDetail(d: UniverseDropped): string {
  return d.rank == null ? tierLabel(d.tier) : `#${d.rank} in ${tierLabel(d.tier)}`;
}

// ---------------------------------------------------------------------------
// D59: Overview "Today's Pick" (the fast-changing tiers: discovery + trending)
// ---------------------------------------------------------------------------

/** The fast-changing tiers the Overview widget shows, in active-list order (D58). */
export const PICK_TIERS = ["discovery", "trending"] as const;
export type PickTier = (typeof PICK_TIERS)[number];
export const PICK_TOP = 10;

export interface PickSection {
  tier: PickTier;
  label: string;
  /** Active members by rank, at most PICK_TOP. */
  rows: UniverseActive[];
  /** Active members of the tier (before the top-N cut). */
  active: number;
  /** Names the tier lost past the active-list cap today. */
  cut: number;
}

/** Top N active names per fast tier, from the stored resolve (nothing re-derived). */
export function pickSections(
  u: Pick<Universe, "active" | "tail_cuts" | "dropped">,
  top: number = PICK_TOP,
): PickSection[] {
  const cuts = u.tail_cuts?.length ? u.tail_cuts : (u.dropped ?? []).filter((d) => d.reason === "over_active_cap");
  return PICK_TIERS.map((tier) => {
    const all = membersOf(u.active, tier);
    return { tier, label: tierLabel(tier), rows: all.slice(0, top), active: all.length, cut: cuts.filter((d) => d.tier === tier).length };
  });
}

/** Column header: `Trending (10 of 17)` = names shown of names in the active list. */
export function pickHeader(s: Pick<PickSection, "label" | "rows" | "active">): string {
  return `${s.label} (${s.rows.length} of ${s.active})`;
}

const SOURCE_WORDS: Record<string, string> = { reddit: "Reddit", stocktwits: "Stocktwits", scout: "Scout", youtube: "YouTube" };
const sourceWord = (w: string) => SOURCE_WORDS[w.toLowerCase()] ?? (w ? w[0]!.toUpperCase() + w.slice(1) : w);

/** One Overview row: `Reddit + Stocktwits`, `Reddit #5 · Stocktwits #2`, score `0.98`. */
export function pickRow(m: Pick<UniverseActive, "source" | "reason">): { source: string; detail: string; score: string | null } {
  const source = (m.source || "").split("+").filter(Boolean).map(sourceWord).join(" + ") || "—";
  const match = /\s*·?\s*score\s+([0-9.]+)\s*$/.exec(m.reason ?? "");
  const rest = match ? (m.reason ?? "").slice(0, match.index) : (m.reason ?? "");
  const detail = rest.replace(/\b(reddit|stocktwits|scout|youtube)\b/gi, (w) => sourceWord(w)).trim();
  return { source, detail, score: match ? match[1]! : null };
}
