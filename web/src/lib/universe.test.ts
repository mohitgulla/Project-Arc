// E12.6: Universe page helpers (summary line, tier order, chip detail, override warning).
import { describe, expect, it } from "vitest";

import { defaultPickTier, discoveryFillLine, pickCaption, pickRow, pickSections, tailCutDetail } from "./universe";

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
  tierCounts,
  tierLabel,
  type UniverseActive,
  type UniverseTier,
} from "./universe";

function tier(name: string, active: number, extra: Partial<UniverseTier> = {}): UniverseTier {
  return {
    name,
    offered: active,
    active,
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
  return { ticker, tier: t, rank, source: "x", reason: "", also_in: [], ...extra };
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

  it("shows source, reason and also-in on a chip", () => {
    const aapl = m("AAPL", "core", 2, { source: "settings", reason: "core list", also_in: ["momentum", "discovery"] });
    expect(memberDetail(aapl)).toEqual(["#2 in Core · source settings", "core list", "also in Momentum, Discovery"]);
    expect(memberDetail(m("Z", "discovery", 1, { reason: "" }))).toHaveLength(1);
  });

  it("formats tier counts, drop reasons and the market reference", () => {
    expect(tierCounts(tier("momentum", 17, { offered: 24, size_cap: 25 }))).toBe("offered 24 · active 17 / 25");
    expect(tierCounts(tier("discovery", 8, { offered: 10 }))).toBe("offered 10 · active 8");
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
  const m = (ticker: string, tier: string, rank: number) => ({ ticker, tier, rank, source: "s", reason: "r", also_in: [] });
  const u = {
    active: [
      m("NVDA", "core", 1),
      ...Array.from({ length: 12 }, (_, i) => m(`D${i + 1}`, "discovery", 12 - i)),
      m("PENG", "trending", 2),
      m("TEM", "trending", 1),
    ],
    tail_cuts: [
      { ticker: "X", tier: "trending", reason: "over_active_cap", rank: 9 },
      { ticker: "Y", tier: "trending", reason: "over_active_cap", rank: 10 },
    ],
    dropped: [],
  };
  it("lists discovery then trending, top 10 by rank, with cut counts", () => {
    const [d, tr] = pickSections(u);
    expect(d!.tier).toBe("discovery");
    expect(d!.label).toBe("Discovery");
    expect(d!.rows.map((r) => r.ticker)).toEqual(["D12", "D11", "D10", "D9", "D8", "D7", "D6", "D5", "D4", "D3"]);
    expect(d!.active).toBe(12);
    expect(tr!.rows.map((r) => r.ticker)).toEqual(["TEM", "PENG"]);
    expect(tr!.cut).toBe(2);
    expect(pickCaption(d!)).toBe("2 cut by top 10 cap");
    expect(pickCaption(tr!)).toBeNull();
  });
  it("never includes core or momentum; None today when empty; first non-empty tab", () => {
    const secs = pickSections({ active: [m("NVDA", "core", 1), m("BULL", "trending", 1)], tail_cuts: [], dropped: [] });
    expect(secs[0]!.rows).toEqual([]);
    expect(pickCaption(secs[0]!)).toBeNull();
    expect(defaultPickTier(secs)).toBe("trending");
    expect(defaultPickTier(pickSections({ active: [], tail_cuts: [], dropped: [] }))).toBe("discovery");
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
  });
});
