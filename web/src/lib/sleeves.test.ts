import { describe, expect, it } from "vitest";

import { apiQuery, perfQuery, sleeveTitle, tradesLink, VERDICT_LABEL, type SleeveStats } from "./performance";
import { ALL_SLEEVES, activeSleeve, sleeveName, sleeveOptions, sleeveUsageLine, sleeveWindow, type SleeveMeta } from "./sleeves";
import { activeFilterCount, tradeQuery } from "./trades";

const META: SleeveMeta[] = [
  { name: "short", label: "Short", dte_min: 10, dte_max: 29, max_positions: 5, exit_policy: "short", exits: [] },
  { name: "swing", label: "Swing", dte_min: 30, dte_max: 60, max_positions: 5, exit_policy: null, exits: [] },
];

describe("sleeve filter model (E22.5, D97)", () => {
  it("offers All · Short · Swing, and nothing for a single-window profile", () => {
    expect(sleeveOptions(META).map((o) => o.label)).toEqual(["All", "Short", "Swing"]);
    expect(sleeveOptions([])).toEqual([]);
    expect(sleeveOptions(undefined)).toEqual([]);
  });

  it("shows Other only while it is the active filter", () => {
    expect(sleeveOptions(META, "other").map((o) => o.label)).toEqual(["All", "Short", "Swing", "Other"]);
    expect(activeSleeve("other", META)).toBe("other");
    expect(activeSleeve("other", [])).toBe(ALL_SLEEVES);
  });

  it("reads an unknown sleeve as All", () => {
    expect(activeSleeve("short", META)).toBe("short");
    expect(activeSleeve("bogus", META)).toBe(ALL_SLEEVES);
    expect(activeSleeve(null, META)).toBe(ALL_SLEEVES);
  });

  it("labels windows, slots and usage", () => {
    expect(sleeveWindow(META[0]!)).toBe("Short 10–29 DTE · 5 slots");
    expect(sleeveWindow({ ...META[1]!, max_positions: null })).toBe("Swing 30–60 DTE · no slot cap");
    expect(sleeveName("other")).toBe("Other");
    expect(
      sleeveUsageLine([
        { name: "short", label: "Short", open: 3, max_positions: 5 },
        { name: "swing", label: "Swing", open: 2, max_positions: 5 },
        { name: "other", label: "Other", open: 1, max_positions: null },
      ]),
    ).toBe("Short 3/5 · Swing 2/5 · Other 1");
    expect(sleeveUsageLine([])).toBeNull();
  });
});

describe("performance sleeve query", () => {
  it("sends ?sleeve= only when set", () => {
    expect(apiQuery(perfQuery(new URLSearchParams("")))).toEqual({ preset: "90d" });
    expect(apiQuery(perfQuery(new URLSearchParams("sleeve=short&range=1M")))).toEqual({
      preset: "30d",
      sleeve: "short",
    });
    expect(perfQuery(new URLSearchParams("sleeve=Bad Value")).sleeve).toBeNull();
  });

  it("carries the sleeve into breakdown links", () => {
    const item = { key: "AAPL", label: "AAPL", count: 1, wins: 1, pnl: 1, win_rate: 1, share: 1, filter: { ticker: "AAPL", stage: "closed" } };
    const period = { first: "2026-10-01", last: "2026-10-12", slot_end: "2026-10-12", days: 12 };
    expect(tradesLink(item, period, "swing")).toContain("sleeve=swing");
    expect(tradesLink({ ...item, filter: { sleeve: "short", stage: "closed" } }, period, "swing")).toContain("sleeve=short");
  });

  it("titles sleeves and verdicts in Title Case", () => {
    const s = { sleeve: "short", dte_min: 10, dte_max: 29 } as SleeveStats;
    expect(sleeveTitle(s)).toBe("Short 10–29 DTE");
    expect(sleeveTitle({ ...s, sleeve: "other", dte_min: null, dte_max: null })).toBe("Other");
    expect(VERDICT_LABEL.shrink).toBe("Shrink To 2 Slots");
  });
});

describe("trades sleeve filter", () => {
  it("passes ?sleeve= to the API and counts it", () => {
    const params = new URLSearchParams("sleeve=swing");
    expect(tradeQuery(params).query).toEqual({ sleeve: "swing" });
    expect(activeFilterCount(params)).toBe(1);
  });
});
