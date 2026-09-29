// One test block per rule in docs/TOWER_DESIGN.md §3.
import { describe, expect, it } from "vitest";

import {
  METRIC_POLARITY,
  changePill,
  formatAge,
  formatAsOf,
  formatAxisMoney,
  formatEt,
  formatLeg,
  formatMoney,
  formatNumber,
  formatPercent,
  formatTotalReturn,
  isStale,
  moneyDecimals,
  moneyParts,
  parseOcc,
  toneFor,
} from "./format";

describe("§3 money: commas, glyph, cents vs whole dollars, leading minus", () => {
  it("adds thousands commas", () => {
    expect(formatMoney(1234567.891, "pnl")).toBe("$1,234,567.89");
    expect(formatMoney(100500, "equity")).toBe("$100,500");
  });

  it("splits the $ glyph so <Money> can render it smaller and raised", () => {
    expect(moneyParts(-7260.02, "pnl")).toEqual({ sign: "-", glyph: "$", number: "7,260.02" });
  });

  it("uses cents on prices, fills and P&L", () => {
    for (const k of ["price", "fill", "pnl"] as const) expect(moneyDecimals(k)).toBe(2);
    expect(formatMoney(0.9, "fill")).toBe("$0.90");
    expect(formatMoney(12.5, "pnl")).toBe("$12.50");
  });

  it("uses whole dollars on equity, max loss and allocation", () => {
    for (const k of ["equity", "max_loss", "allocation", "buying_power"] as const)
      expect(moneyDecimals(k)).toBe(0);
    expect(formatMoney(100499.6, "equity")).toBe("$100,500");
    expect(formatMoney(140.4, "max_loss")).toBe("$140");
  });

  it("writes negatives with a leading minus, never parentheses", () => {
    expect(formatMoney(-7260.02, "pnl")).toBe("-$7,260.02");
    expect(formatMoney(-7260.02, "pnl")).not.toMatch(/[()]/);
  });

  it("never shows -$0.00", () => {
    expect(formatMoney(-0.001, "pnl")).toBe("$0.00");
    expect(formatMoney(-0.4, "equity")).toBe("$0");
  });

  it("adds + only on request", () => {
    expect(formatMoney(5, "pnl")).toBe("$5.00");
    expect(formatMoney(5, "pnl", { explicitSign: true })).toBe("+$5.00");
    expect(formatMoney(0, "pnl", { explicitSign: true })).toBe("$0.00");
  });
});

describe("§3 axis ticks: $71K, -$9K, $1.2M", () => {
  it.each([
    [71_000, "$71K"],
    [-9_000, "-$9K"],
    [1_200_000, "$1.2M"],
    [1_500, "$1.5K"],
    [950, "$950"],
    [0, "$0"],
    [-0.2, "$0"],
    [999_950, "$1M"],
    [2_500_000_000, "$2.5B"],
    [10_400, "$10K"],
  ])("%d -> %s", (v, s) => {
    expect(formatAxisMoney(v)).toBe(s);
  });
});

describe("§3 percent: 2 decimals below 100 %, 1 at/above", () => {
  it.each([
    [0.0125, "1.25%"],
    [0.5979, "59.79%"],
    [1.481, "148.1%"],
    [1, "100.0%"],
    [0.99999, "100.0%"],
    [-0.5979, "-59.79%"],
    [0, "0.00%"],
    [-0.00001, "0.00%"],
  ])("%d -> %s", (v, s) => {
    expect(formatPercent(v)).toBe(s);
  });

  it("adds + only on request", () => {
    expect(formatPercent(0.1, { explicitSign: true })).toBe("+10.00%");
  });
});

describe("§3 change pill: glyph + unsigned value, colour = favourability", () => {
  it("shows ↗ 148.1% for an up move, no sign", () => {
    const p = changePill(1.481, "pnl");
    expect(p).toEqual({ direction: "up", glyph: "↗", text: "148.1%", tone: "pos" });
  });

  it("shows ↘ with an unsigned value for a down move", () => {
    const p = changePill(-0.0512, "equity");
    expect(p.glyph).toBe("↘");
    expect(p.text).toBe("5.12%");
    expect(p.text).not.toContain("-");
    expect(p.tone).toBe("neg");
  });

  it("colours up = green for P&L, equity, PoP, net EV, win rate", () => {
    for (const m of ["pnl", "equity", "pop", "net_ev", "win_rate"] as const) {
      expect(changePill(0.1, m).tone).toBe("pos");
      expect(changePill(-0.1, m).tone).toBe("neg");
    }
  });

  it("colours up = red for costs, slippage, spend, drawdown, max loss, violations, latency, LLM cost", () => {
    for (const m of [
      "cost",
      "slippage",
      "spend",
      "drawdown",
      "max_loss",
      "gate_violations",
      "latency",
      "llm_cost",
    ] as const) {
      expect(changePill(0.1, m).tone).toBe("neg");
      expect(changePill(-0.1, m).tone).toBe("pos");
    }
  });

  it("leaves neutral quantities uncoloured", () => {
    for (const m of ["contracts", "count", "delta_exposure"] as const) {
      expect(changePill(3, m).tone).toBe("neutral");
      expect(changePill(-3, m).tone).toBe("neutral");
    }
  });

  it("covers every metric in the rule table", () => {
    expect(Object.keys(METRIC_POLARITY)).toHaveLength(16);
  });

  it("accepts a custom formatter and treats a value that rounds to zero as flat", () => {
    const money = changePill(-312, "pnl", (a) => formatMoney(a, "pnl"));
    expect(money.text).toBe("$312.00");
    expect(money.glyph).toBe("↘");
    const flat = changePill(0.000001, "pnl");
    expect(flat).toMatchObject({ direction: "flat", glyph: "", tone: "neutral" });
    expect(toneFor("flat", "up_good")).toBe("neutral");
  });

  it("accepts a raw polarity for unlisted metrics", () => {
    expect(changePill(1, "up_bad").tone).toBe("neg");
  });
});

describe("§3 total return on a detail panel: explicit sign, parenthesised %, neutral", () => {
  it("formats -$25,599.00 (-59.79%)", () => {
    expect(formatTotalReturn(-25599, -0.5979)).toBe("-$25,599.00 (-59.79%)");
    expect(formatTotalReturn(1200.5, 0.012)).toBe("+$1,200.50 (+1.20%)");
  });
});

describe("§3 times: ET, `Mon 09-28 15:35`, ages", () => {
  it("formats in ET regardless of the input offset", () => {
    expect(formatEt("2026-09-28T19:35:00Z")).toBe("Mon 09-28 15:35");
    expect(formatEt("2026-09-28T15:35:00-04:00")).toBe("Mon 09-28 15:35");
    expect(formatEt("2026-12-01T14:30:00Z")).toBe("Tue 12-01 09:30"); // EST
    expect(formatEt("nope")).toBe("—");
  });

  const now = new Date("2026-09-28T19:40:00Z");
  it.each([
    ["2026-09-28T19:39:30Z", "just now"],
    ["2026-09-28T19:36:00Z", "4m ago"],
    ["2026-09-28T17:40:00Z", "2h ago"],
    ["2026-09-25T19:40:00Z", "3d ago"],
    ["2026-09-28T19:45:00Z", "just now"],
    ["garbage", "—"],
  ])("%s -> %s", (t, s) => {
    expect(formatAge(t, now)).toBe(s);
  });

  it("is stale past 3x the producing job's cadence", () => {
    const cadence = 300; // 5 min tick
    expect(isStale("2026-09-28T19:26:00Z", cadence, now)).toBe(false); // 14m
    expect(isStale("2026-09-28T19:25:00Z", cadence, now)).toBe(false); // exactly 15m
    expect(isStale("2026-09-28T19:24:59Z", cadence, now)).toBe(true);
    expect(isStale(null, cadence, now)).toBe(true);
  });

  it("builds the as-of line, prefixed `stale` when stale", () => {
    expect(formatAsOf("2026-09-28T19:36:00Z", now, 300)).toBe("as of Mon 09-28 15:36 · 4m ago");
    expect(formatAsOf("2026-09-28T17:40:00Z", now, 300)).toBe(
      "stale · as of Mon 09-28 13:40 · 2h ago",
    );
    expect(formatAsOf("2026-09-28T17:40:00Z", now)).toBe("as of Mon 09-28 13:40 · 2h ago");
  });
});

describe("§3 option legs: `AMD 10/02 620C`", () => {
  it("formats OCC symbols", () => {
    expect(formatLeg("AMD261002C00620000")).toBe("AMD 10/02 620C");
    expect(formatLeg("SPY261030P00711000")).toBe("SPY 10/30 711P");
    expect(formatLeg("SPY261030P00711500")).toBe("SPY 10/30 711.5P");
    expect(formatLeg("BRK.B261016C00450000")).toBe("BRK.B 10/16 450C");
  });

  it("parses the OCC fields and falls back to the raw symbol", () => {
    expect(parseOcc("AMD261002C00620000")).toEqual({
      root: "AMD",
      expiration: "2026-10-02",
      right: "C",
      strike: 620,
    });
    expect(parseOcc("SPY")).toBeNull();
    expect(formatLeg("SPY")).toBe("SPY");
  });
});

describe("neutral numbers", () => {
  it("formats with commas and bounded decimals", () => {
    expect(formatNumber(1234.567, 1)).toBe("1,234.6");
    expect(formatNumber(2)).toBe("2");
  });
});
