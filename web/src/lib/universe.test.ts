// E12.6: Universe page helpers (summary line, tier order, chip detail, override warning).
import { describe, expect, it } from "vitest";

import {
  alsoInTitle,
  carriedTitle,
  discoveryFillLine,
  droppedCount,
  GRID_COLS,
  gridRows,
  inputsLabel,
  moreLabel,
  PICK_LEGEND_INFO,
  pickHeader,
  pickRow,
  pickScore,
  pickSections,
  pillFacts,
  pillScore,
  rowDetail,
  sourcesLabel,
  stancePill,
  tailCutDetail,
  tickerParam,
  todayPrev,
  universeHref,
  weightText,
} from "./universe";

import {
  CORE_KEY_LABEL,
  coreOverrideWarning,
  dropLabel,
  marketReferenceLine,
  memberDetail,
  membersOf,
  refreshLine,
  resolveLabel,
  sortTiers,
  summaryLine,
  tierActiveLine,
  tierCountLine,
  tierLabel,
  type UniverseActive,
  type UniverseTier,
} from "./universe";

function tier(name: string, active: number, extra: Partial<UniverseTier> = {}): UniverseTier {
  return {
    name,
    listed: active,
    active,
    carried: 0,
    size_cap: null,
    source: null,
    url: null,
    fetched_at: null,
    age_s: null,
    source_as_of: null,
    partial: false,
    expired: false,
    ...extra,
  };
}

function m(ticker: string, t: string, rank: number, extra: Partial<UniverseActive> = {}): UniverseActive {
  return { ticker, tier: t, rank, source: "x", reason: "", also_in: [], carried: false, picked_20d: 0, proposals_20d: 0, ...extra };
}

describe("universe page helpers", () => {
  it("builds the pinned summary in tier order", () => {
    const tiers = [tier("discovery", 8), tier("core", 25), tier("momentum", 17)];
    const active = Array.from({ length: 50 }, (_, i) => m(`T${i}`, "core", i + 1));
    expect(summaryLine({ active, active_max: 50, tiers })).toBe("Active 50/50 · Core 25 · Momentum 17 · Discovery 8");
    expect(sortTiers(tiers).map((t) => t.name)).toEqual(["core", "momentum", "discovery"]);
  });

  it("keeps an unknown tier last", () => {
    expect(sortTiers([tier("zeta", 1), tier("core", 1)]).map((t) => t.name)).toEqual(["core", "zeta"]);
  });

  it("lists a tier's members by rank", () => {
    const a = [m("B", "momentum", 2), m("X", "core", 1), m("A", "momentum", 1)];
    expect(membersOf(a, "momentum").map((x) => x.ticker)).toEqual(["A", "B"]);
  });

  it("shows source, reason and also-in on a pill's title (no Stocktwits, D67)", () => {
    const aapl = m("AAPL", "core", 2, { source: "settings", reason: "core list", also_in: ["momentum", "discovery"] });
    expect(memberDetail(aapl)).toEqual(["#2 in Core · source settings", "core list", "also in Momentum, Discovery"]);
    expect(memberDetail(m("Z", "discovery", 1, { reason: "" }))).toHaveLength(1);
    expect(memberDetail(m("N", "core", 1, { reason: "", sentiment: "ST 80% bull (10 tagged, 2.7h)" }))).toEqual(["#1 in Core · source x"]);
  });

  it("formats tier counts, drop reasons and the market reference", () => {
    expect(tierCountLine(tier("momentum", 17, { listed: 24, size_cap: 20 }))).toBe("top 20 of 24 listed");
    expect(tierCountLine(tier("momentum", 11, { listed: 12, size_cap: 20 }))).toBe("top 12 of 12 listed");
    expect(tierCountLine(tier("core", 20))).toBe("20 names");
    expect(tierCountLine(tier("discovery", 8, { listed: 10, carried: 3 }))).toBe("10 names (3 carried)");
    expect(tierCountLine(tier("trending", 14, { listed: 14 }))).toBe("14 names");
    expect(tierActiveLine(tier("momentum", 11, { size_cap: 20 }))).toBe("active 11 / 20");
    expect(tierActiveLine(tier("discovery", 8))).toBe("active 8");
    for (const n of ["core", "momentum", "discovery", "trending"]) expect(tierCountLine(tier(n, 5, { size_cap: 20 }))).not.toMatch(/offered/);
    expect(dropLabel("over_active_cap")).toBe("past the active-list cap");
    expect(dropLabel("weird_reason")).toBe("weird reason");
    expect(marketReferenceLine(["SPY", "QQQ"])).toBe("SPY QQQ (regime only, not traded)");
    expect(marketReferenceLine([])).toBe("none");
    expect(tierLabel("discovery")).toBe("Discovery");
  });

  it("labels the resolve state", () => {
    expect(resolveLabel({ state: "today", resolved_for: "2026-10-05" })).toBe("Resolved Today");
    expect(resolveLabel({ state: "stale", resolved_for: "2026-10-04" })).toBe("Not Resolved Today · last 2026-10-04");
    expect(resolveLabel({ state: "none", resolved_for: null })).toBe("Never Resolved");
  });

  it("warns only when the core override has more than 30 names", () => {
    expect(CORE_KEY_LABEL).toBe("Core Universe (≤ 30)");
    expect(coreOverrideWarning(Array.from({ length: 30 }, (_, i) => `T${i}`))).toBeNull();
    expect(coreOverrideWarning(Array.from({ length: 60 }, (_, i) => `T${i}`))).toMatch(/^60 names > 30/);
    expect(coreOverrideWarning(["a", "A", "$a"])).toBeNull(); // deduped like the resolver
    expect(coreOverrideWarning("NVDA")).toBeNull();
  });

  it("names the refreshing jobs (D56)", () => {
    expect(refreshLine()).toBe(
      "momentum refreshes on its own job; discovery is written by the Scout; trending is ranked daily from Reddit + Stocktwits.",
    );
  });
});

describe("E13.14 tail cuts + discovery fill", () => {
  it("shows the Scout fill only under d56", () => {
    const tiers = [{ name: "discovery", size_cap: 20 }] as never;
    expect(discoveryFillLine({ discovery_fill: 8, tiers })).toBe("Discovery fill 8 / 20 today (Scout)");
    expect(discoveryFillLine({ discovery_fill: 0, tiers: [] })).toBe("Discovery fill 0 today (Scout)");
    expect(discoveryFillLine({ discovery_fill: null, tiers })).toBeNull();
  });

  it("labels a tail cut with its rank", () => {
    expect(tailCutDetail({ ticker: "TEM", tier: "discovery", reason: "over_active_cap", rank: 18 })).toBe("#18 in Discovery");
    expect(tailCutDetail({ ticker: "TEM", tier: "momentum", reason: "over_active_cap", rank: null })).toBe("Momentum");
  });
});

describe("D59 Today's Pick", () => {
  const m = (ticker: string, tier: string, rank: number) => ({ ticker, tier, rank, source: "s", reason: "r", also_in: [], carried: false, picked_20d: 0, proposals_20d: 0 });
  const u = {
    active: [
      m("NVDA", "core", 1),
      ...Array.from({ length: 14 }, (_, i) => m(`D${i + 1}`, "discovery", 14 - i)),
      m("PENG", "trending", 2),
      m("TEM", "trending", 1),
    ],
    tail_cuts: [
      { ticker: "X", tier: "trending", reason: "over_active_cap", rank: 9 },
      { ticker: "Y", tier: "trending", reason: "over_active_cap", rank: 10 },
    ],
    dropped: [],
  };
  it("lists discovery then trending, top 12 by rank, with more and cut counts", () => {
    const [d, tr] = pickSections(u);
    expect(d!.tier).toBe("discovery");
    expect(d!.label).toBe("Discovery");
    expect(d!.rows.map((r) => r.ticker)).toEqual(["D14", "D13", "D12", "D11", "D10", "D9", "D8", "D7", "D6", "D5", "D4", "D3"]);
    expect(d!.active).toBe(14);
    expect(d!.more).toBe(2);
    expect(moreLabel(d!.more)).toBe("+2 more");
    expect(tr!.rows.map((r) => r.ticker)).toEqual(["TEM", "PENG"]);
    expect(tr!.more).toBe(0);
    expect(moreLabel(tr!.more)).toBeNull();
    expect(tr!.cut).toBe(2);
    expect(pickHeader(d!)).toBe("Discovery (14)");
    expect(pickHeader(tr!)).toBe("Trending (2)");
  });
  it("never includes core or momentum; an empty tier reads (0)", () => {
    const secs = pickSections({ active: [m("NVDA", "core", 1), m("BULL", "trending", 1)], tail_cuts: [], dropped: [] });
    expect(secs[0]!.rows).toEqual([]);
    expect(pickHeader(secs[0]!)).toBe("Discovery (0)");
  });
  it("falls back to over_active_cap drops when tail_cuts is absent", () => {
    const [, tr] = pickSections({ active: [], tail_cuts: [], dropped: [{ ticker: "Z", tier: "trending", reason: "over_active_cap", rank: null }] });
    expect(tr!.cut).toBe(1);
  });
  it("formats a row: source words, detail without the score, score apart", () => {
    expect(pickRow({ source: "reddit+stocktwits", reason: "reddit #5 · stocktwits #2 · score 0.98" })).toEqual({
      source: "Reddit + Stocktwits",
      detail: "Reddit #5 · Stocktwits #2",
      score: "0.98",
    });
    expect(pickRow({ source: "scout", reason: "2 videos" })).toEqual({ source: "Scout", detail: "2 videos", score: null });
    expect(pickRow({ source: "scout", reason: "bullish · stockedup · score 0.72" }).score).toBe("0.72");
  });
});

describe("E14.8 (D64) score and per-name values", () => {
  it("uses the combined score and falls back to the reason for pre-D64 rows", () => {
    expect(pickScore({ score: 0.9812, source: "s", reason: "reddit #3 · score 0.50" })).toBe("0.98");
    expect(pickScore({ score: null, source: "s", reason: "reddit #3 · stocktwits #1 · score 0.96" })).toBe("0.96");
    expect(pickScore({ source: "scout", reason: "2 videos" })).toBeNull();
  });

  it("formats Today / Prev with carried and new names", () => {
    expect(todayPrev({ score_today: 0.6, score_prev: 0.7 })).toBe("0.60 / 0.70");
    expect(todayPrev({ score_today: null, score_prev: 0.7 })).toBe("— / 0.70");
    expect(todayPrev({ score_today: 0.6 })).toBe("0.60 / —");
  });

  it("tags carried rows in the detail and shows In tier (no Stocktwits line)", () => {
    const c = m("NBIS", "discovery", 3, { carried: true, runs: ["2026-10-07"], in_tier_20d: 4, reason: "YouTube call", sentiment: "ST 50% bull (2 tagged, 1h)" });
    expect(rowDetail(c)).toEqual(["In tier 4 of the last 20 sessions", "carried from 2026-10-07", "YouTube call"]);
    const core = m("NVDA", "core", 1, { reason: "core list", in_tier_20d: null });
    expect(rowDetail(core)).toEqual(["core list"]);
  });

  it("labels trending inputs, discovery sources, momentum weight and stance", () => {
    expect(inputsLabel({ source: "reddit+stocktwits", reason: "reddit #3 · stocktwits #1 · score 0.98" })).toBe("Reddit #3 · Stocktwits #1");
    expect(inputsLabel({ source: "reddit", reason: "reddit #4 · carried from 2026-10-07 · score 0.40" })).toBe("Reddit #4");
    expect(sourcesLabel({ origin_labels: ["Arete Trading", "FX Evolution"] })).toBe("Arete Trading, FX Evolution");
    expect(sourcesLabel({ origin_labels: null })).toBe("—");
    expect(weightText({ weight_pct: 9.48 })).toBe("9.48%");
    expect(weightText({ weight_pct: null })).toBe("—");
    expect(stancePill({ stance: "bullish" })).toEqual({ label: "Bullish", tone: "pos" });
    expect(stancePill({ stance: "bearish" })).toEqual({ label: "Bearish", tone: "neg" });
    expect(stancePill({ stance: null })).toBeNull();
  });
});

describe("E14.10 (D67) pill grids, details and the ?t= deep link", () => {
  it("splits pills into rows of 4 (the last row short)", () => {
    expect(GRID_COLS).toBe(4);
    expect(gridRows([1, 2, 3, 4, 5, 6, 7, 8, 9])).toEqual([[1, 2, 3, 4], [5, 6, 7, 8], [9]]);
    expect(gridRows([1, 2, 3, 4])).toEqual([[1, 2, 3, 4]]);
    expect(gridRows([])).toEqual([]);
    expect(gridRows([1, 2, 3], 2)).toEqual([[1, 2], [3]]);
  });

  it("parses the ?t= ticker param", () => {
    expect(tickerParam("?t=nvda")).toBe("NVDA");
    expect(tickerParam("t=%24TSM")).toBe("TSM");
    expect(tickerParam(new URLSearchParams({ t: "BRK.B" }))).toBe("BRK.B");
    expect(tickerParam("?t=")).toBeNull();
    expect(tickerParam("")).toBeNull();
    expect(tickerParam("?t=<script>")).toBeNull();
    expect(tickerParam("?t=WAYTOOLONGTICKER")).toBeNull();
    expect(universeHref("BRK.B")).toBe("/ops/universe?t=BRK.B");
    expect(tickerParam(universeHref("GCT").split("?")[1]!)).toBe("GCT");
  });

  it("shows a score on the fast tiers only", () => {
    expect(pillScore(m("A", "discovery", 1, { score: 0.618 }))).toBe("0.62");
    expect(pillScore(m("B", "trending", 1, { score: 0.5 }))).toBe("0.50");
    expect(pillScore(m("C", "trending", 1, { score: null, reason: "" }))).toBeNull();
    expect(pillScore(m("D", "core", 1, { score: 0.9 }))).toBeNull();
    expect(pillScore(m("E", "momentum", 1, { score: 0.9 }))).toBeNull();
  });

  it("titles the carried and also-in markers", () => {
    expect(carriedTitle(m("A", "discovery", 1, { carried: true, runs: ["2026-10-08"] }))).toBe("Carried from 2026-10-08");
    expect(carriedTitle(m("A", "discovery", 1, { carried: true, runs: null }))).toBe("Carried from the previous run");
    expect(carriedTitle(m("A", "discovery", 1))).toBeNull();
    expect(alsoInTitle(m("A", "discovery", 1, { also_in: ["trending"] }))).toBe("Also in Trending");
    expect(alsoInTitle(m("A", "discovery", 1))).toBeNull();
  });

  it("lists every former column per tier in the detail panel, never ST", () => {
    const keys = (x: UniverseActive) => pillFacts(x).map((f) => f.key);
    expect(keys(m("NVDA", "core", 1))).toEqual(["rank", "picked", "trades"]);
    expect(keys(m("AVGO", "momentum", 2, { weight_pct: 9.48 }))).toEqual(["rank", "picked", "trades", "weight"]);
    expect(keys(m("QCOM", "discovery", 3, { stance: "bullish" }))).toEqual(["rank", "picked", "trades", "score", "today-prev", "stance", "sources"]);
    expect(keys(m("QCOM", "discovery", 3))).toEqual(["rank", "picked", "trades", "score", "today-prev", "sources"]);
    expect(keys(m("VZ", "trending", 1))).toEqual(["rank", "picked", "trades", "score", "today-prev", "inputs"]);
    const q = pillFacts(
      m("QCOM", "discovery", 3, {
        score: 0.86,
        score_today: 0.86,
        score_prev: 0.7,
        stance: "bullish",
        origin_labels: ["Arete Trading"],
        picked_20d: 4,
        proposals_20d: 2,
        sentiment_bull_pct: 80,
      }),
    );
    const v = Object.fromEntries(q.map((f) => [f.key, f.value]));
    expect(v).toEqual({ rank: "#3 in Discovery", picked: "4", trades: "2", score: "0.86", "today-prev": "0.86 / 0.70", stance: "Bullish", sources: "Arete Trading" });
    expect(JSON.stringify(q)).not.toMatch(/% bull|Stocktwits/);
    expect(pillFacts(m("AVGO", "momentum", 2, { weight_pct: 9.48 })).find((f) => f.key === "weight")!.value).toBe("9.48%");
  });

  it("counts tail cuts and drops once each for the disclosure", () => {
    const d = (ticker: string, tier: string, reason = "over_active_cap") => ({ ticker, tier, reason, rank: null });
    expect(droppedCount({ tail_cuts: [d("A", "trending"), d("B", "discovery")], dropped: [d("A", "trending"), d("K", "momentum", "over_tier_size")] })).toBe(3);
    expect(droppedCount({ tail_cuts: [], dropped: [] })).toBe(0);
  });

  it("drops ST from the Today's Pick legend", () => {
    expect(PICK_LEGEND_INFO).not.toMatch(/\bST\b|Stocktwits/);
  });
});
