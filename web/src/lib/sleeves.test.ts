import { describe, expect, it } from "vitest";

import { apiQuery, perfQuery, sleeveMetrics, sleeveTitle, tradesLink, type SleeveStats } from "./performance";
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

  it("shows six sleeve metrics, 3 + 3, with no median, CI, exits or verdict", () => {
    const s = {
      sleeve: "short", dte_min: 10, dte_max: 35, mean_pnl: -100, median_pnl: -80, mean_net_pnl: -100.37,
      ci_low: -200.63, ci_high: -0.11, mean_pct_of_max_loss: -0.1894, profit_factor: 0.42, mean_days_held: 1.3,
      entry_slippage: 26.5, mean_mae: -135.83, mean_mfe: 80.83, exit_reasons: { stop: 1 }, verdict: "collect", ready: false,
    } as unknown as SleeveStats;
    const m = sleeveMetrics(s);
    expect(m.map((x) => x.label)).toEqual(["Mean", "P&L", "Profit factor", "Days held", "Slippage", "MAE / MFE"]);
    expect(m[1]?.value).toMatch(/%$/);
    expect(m[2]?.value).toBe("0.42");
    expect(sleeveMetrics({ ...s, profit_factor: null }).find((x) => x.label === "Profit factor")?.value).toBe("—");
    expect(JSON.stringify(m)).not.toMatch(/median|CI|Exits|Collecting|Ready/i);
  });

  it("titles sleeves in Title Case", () => {
    const s = { sleeve: "short", dte_min: 10, dte_max: 35 } as SleeveStats;
    expect(sleeveTitle(s)).toBe("Short 10–35 DTE");
    expect(sleeveTitle({ ...s, sleeve: "other", dte_min: null, dte_max: null })).toBe("Other");
  });
});

describe("trades sleeve filter", () => {
  it("passes ?sleeve= to the API and counts it", () => {
    const params = new URLSearchParams("sleeve=swing");
    expect(tradeQuery(params).query).toEqual({ sleeve: "swing" });
    expect(activeFilterCount(params)).toBe(1);
  });
});
