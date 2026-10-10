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

/** A pill's hover title lines: source, reason, `also in Momentum, Discovery`. */
export function memberDetail(m: UniverseActive): string[] {
  const out = [`#${m.rank} in ${tierLabel(m.tier)} · source ${m.source || "—"}`];
  if (m.inputs != null) out.push(m.inputs >= 2 ? "in both inputs" : "in one input");
  if (m.velocity_detail) out.push(`mention velocity: ${m.velocity_detail}`);
  if (m.reason) out.push(m.reason);
  const also = m.also_in ?? [];
  if (also.length) out.push(`also in ${also.map(tierLabel).join(", ")}`);
  return out;
}

/** E14.8 (D64) tier header count, replacing the old `offered n` caption:
 *  core `20 names`; momentum `top 20 of 24 listed` (tier size vs the feed's rows);
 *  discovery / trending `25 names (3 carried)`. */
export function tierCountLine(t: Pick<UniverseTier, "name" | "listed" | "active" | "size_cap" | "carried">): string {
  if (t.name === "momentum") {
    if (t.size_cap == null) return `${t.listed} listed`;
    return `top ${Math.min(t.size_cap, t.listed)} of ${t.listed} listed`;
  }
  if (t.name === "core") return `${t.listed} names`;
  const carried = t.carried ?? 0;
  return `${t.listed} names${carried > 0 ? ` (${carried} carried)` : ""}`;
}

/** `active 11 / 20` (the tier's active-list share vs its size). */
export function tierActiveLine(t: Pick<UniverseTier, "active" | "size_cap">): string {
  return `active ${t.active}${t.size_cap == null ? "" : ` / ${t.size_cap}`}`;
}

// ---------------------------------------------------------------------------
// E14.8 (D64): per-name values for Today's Pick and the Ops › Universe pill details.
// E14.10 (D67): Stocktwits sentiment (ST) is no longer shown on either view.
// ---------------------------------------------------------------------------

export type StanceTone = "pos" | "neg" | "neutral";

/** The combined score, 2 dp; pre-D64 rows fall back to the reason's `score x.xx`. */
export function pickScore(m: Pick<UniverseActive, "score" | "source" | "reason">): string | null {
  if (m.score != null) return m.score.toFixed(2);
  return pickRow(m).score;
}

const fmt2 = (x: number | null | undefined) => (x == null ? "—" : x.toFixed(2));

/** `0.60 / 0.70`, `— / 0.70` (carried), `0.60 / —` (new today). */
export function todayPrev(m: Pick<UniverseActive, "score_today" | "score_prev">): string {
  return `${fmt2(m.score_today)} / ${fmt2(m.score_prev)}`;
}

/** Trending inputs from the reason: `Reddit #3 · Stocktwits #1` (score and carry tag dropped). */
export function inputsLabel(m: Pick<UniverseActive, "source" | "reason">): string {
  const d = pickRow(m).detail.replace(/\s*·?\s*carried from \S+/i, "").trim();
  return d || "—";
}

/** Discovery sources: channel labels `Arete Trading, FX Evolution`. */
export function sourcesLabel(m: Pick<UniverseActive, "origin_labels">): string {
  return m.origin_labels?.length ? m.origin_labels.join(", ") : "—";
}

/** `9.48%` or `—`. */
export function weightText(m: Pick<UniverseActive, "weight_pct">): string {
  return m.weight_pct == null ? "—" : `${m.weight_pct.toFixed(2)}%`;
}

/** `Bullish` / `Bearish` + tone, or null. */
export function stancePill(m: Pick<UniverseActive, "stance">): { label: string; tone: StanceTone } | null {
  if (!m.stance) return null;
  const s = m.stance.toLowerCase();
  return { label: tierLabel(s), tone: s === "bullish" ? "pos" : s === "bearish" ? "neg" : "neutral" };
}

/** Detail-field ⓘ texts (E14.8; E14.10 shows them as the field label's title). */
export const COLUMN_INFO = {
  picked: "Days this name became a trade idea, last 20 sessions.",
  trades: "Proposals built, last 20 sessions.",
  score: "2-run combined score: 0.6 × today + 0.4 × the previous run.",
  weight: "The name's weight in the SPMO momentum ETF.",
} as const;

export const PICK_LEGEND_INFO = "Score: 2-run combined (0.6 today + 0.4 previous run). Tap a name for its details.";

/** Detail lines under a pill's fields: In tier, carried from, velocity, reason, also-in. */
export function rowDetail(m: UniverseActive): string[] {
  const out: string[] = [];
  if (m.in_tier_20d != null) out.push(`In tier ${m.in_tier_20d} of the last 20 sessions`);
  if (m.carried && m.runs?.length) out.push(`carried from ${m.runs[0]}`);
  if (m.velocity_detail) out.push(`mention velocity: ${m.velocity_detail}`);
  if (m.reason) out.push(m.reason);
  const also = m.also_in ?? [];
  if (also.length) out.push(`also in ${also.map(tierLabel).join(", ")}`);
  return out;
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
/** E14.10 (D67): at most 12 pills per tier (3 rows of 4), then `+k more`. */
export const PICK_TOP = 12;

export interface PickSection {
  tier: PickTier;
  label: string;
  /** Active members by rank, at most PICK_TOP. */
  rows: UniverseActive[];
  /** Active members of the tier (before the top-N cut). */
  active: number;
  /** Active names not shown (active − rows): the `+k more` count. */
  more: number;
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
    const rows = all.slice(0, top);
    return { tier, label: tierLabel(tier), rows, active: all.length, more: all.length - rows.length, cut: cuts.filter((d) => d.tier === tier).length };
  });
}

/** Section header: `Trending (14)` = the tier's active count (E14.10). */
export function pickHeader(s: Pick<PickSection, "label" | "active">): string {
  return `${s.label} (${s.active})`;
}

/** `+2 more` under a capped pill grid, or null when every name is shown. */
export function moreLabel(more: number): string | null {
  return more > 0 ? `+${more} more` : null;
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

// ---------------------------------------------------------------------------
// E14.10 (D67): 4-column pill grids, click-through details, `?t=` deep link
// ---------------------------------------------------------------------------

/** Every pill grid has 4 columns at every width. */
export const GRID_COLS = 4;

/** Split items into grid rows of `cols` (a detail panel goes in below its pill's row). */
export function gridRows<T>(items: readonly T[], cols: number = GRID_COLS): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < items.length; i += cols) out.push(items.slice(i, i + cols));
  return out;
}

/** The `?t=` deep-link ticker, upper-cased; null when absent or not ticker-shaped. */
export function tickerParam(search: string | URLSearchParams): string | null {
  const p = typeof search === "string" ? new URLSearchParams(search) : search;
  const t = (p.get("t") ?? "").trim().replace(/^\$/, "").toUpperCase();
  return /^[A-Z0-9][A-Z0-9.-]{0,9}$/.test(t) ? t : null;
}

/** `/ops/universe?t=NVDA` (Today's Pick → that name's Universe panel). */
export function universeHref(ticker: string): string {
  return `/ops/universe?t=${encodeURIComponent(ticker)}`;
}

/** Pill score: on the fast tiers (Discovery, Trending) only. */
export function pillScore(m: Pick<UniverseActive, "tier" | "score" | "source" | "reason">): string | null {
  return m.tier === "discovery" || m.tier === "trending" ? pickScore(m) : null;
}

/** Pill marker titles: `Carried from 2026-10-07`, `Also in Trending`. */
export function carriedTitle(m: Pick<UniverseActive, "carried" | "runs">): string | null {
  if (!m.carried) return null;
  return m.runs?.length ? `Carried from ${m.runs[0]}` : "Carried from the previous run";
}

export function alsoInTitle(m: Pick<UniverseActive, "also_in">): string | null {
  const also = m.also_in ?? [];
  return also.length ? `Also in ${also.map(tierLabel).join(", ")}` : null;
}

export interface PillFact {
  key: string;
  label: string;
  value: string;
  info?: string;
}

/** The detail panel's fields: everything the E14.8 columns showed, except ST. */
export function pillFacts(m: UniverseActive): PillFact[] {
  const out: PillFact[] = [
    { key: "rank", label: "Rank", value: `#${m.rank} in ${tierLabel(m.tier)}` },
    { key: "picked", label: "Picked (20d)", value: String(m.picked_20d ?? 0), info: COLUMN_INFO.picked },
    { key: "trades", label: "Trades (20d)", value: String(m.proposals_20d ?? 0), info: COLUMN_INFO.trades },
  ];
  if (m.tier === "momentum") out.push({ key: "weight", label: "SPMO Weight", value: weightText(m), info: COLUMN_INFO.weight });
  if (m.tier === "discovery" || m.tier === "trending") {
    out.push({ key: "score", label: "Score", value: pickScore(m) ?? "—", info: COLUMN_INFO.score });
    out.push({ key: "today-prev", label: "Today / Prev", value: todayPrev(m) });
  }
  if (m.tier === "discovery") {
    const s = stancePill(m);
    if (s) out.push({ key: "stance", label: "Stance", value: s.label });
    out.push({ key: "sources", label: "Sources", value: sourcesLabel(m) });
  }
  if (m.tier === "trending") out.push({ key: "inputs", label: "Inputs", value: inputsLabel(m) });
  return out;
}

/** Names behind the `Dropped & Reference (n)` disclosure: tail cuts ∪ dropped, by tier + ticker. */
export function droppedCount(u: Pick<Universe, "tail_cuts" | "dropped">): number {
  const keys = new Set<string>();
  for (const d of [...(u.tail_cuts ?? []), ...(u.dropped ?? [])]) keys.add(`${d.tier}:${d.ticker}`);
  return keys.size;
}
