import { describe, expect, it } from "vitest";

import {
  DEFAULT_RANGE,
  EXPLAIN,
  PERF_RANGES,
  RANGE_PRESET,
  apiQuery,
  equityView,
  formatRange,
  perfQuery,
  personaLabel,
  tradesLink,
  withEmoji,
  type Performance,
} from "./performance";

const P = (s: string) => perfQuery(new URLSearchParams(s));

describe("perfQuery", () => {
  it("defaults to 3M (the 90d preset), ticker tab, no shadow", () => {
    expect(DEFAULT_RANGE).toBe("3M");
    expect(P("")).toEqual({ range: "3M", by: "ticker", shadow: false });
    expect(apiQuery(P(""))).toEqual({ preset: "90d" });
  });

  it("maps every range to an API preset and sends only the preset", () => {
    expect(PERF_RANGES).toEqual(["1D", "1W", "1M", "3M", "YTD", "ALL"]);
    expect(RANGE_PRESET).toEqual({ "1D": "1d", "1W": "7d", "1M": "30d", "3M": "90d", YTD: "ytd", ALL: "all" });
    for (const r of PERF_RANGES) expect(apiQuery(P(`range=${r}`))).toEqual({ preset: RANGE_PRESET[r] });
  });

  it("reads the breakdown tab and shadow from the URL; ignores the removed params", () => {
    const q = P("range=YTD&by=regime&shadow=true&include_tests=true&preset=week");
    expect(q).toEqual({ range: "YTD", by: "regime", shadow: true });
    expect(apiQuery(q)).toEqual({ preset: "ytd" });
  });

  it("falls back on junk", () => {
    expect(P("range=2Y&by=y")).toEqual({ range: "3M", by: "ticker", shadow: false });
  });
});

describe("explanations", () => {
  it("every on-card sub-text is one short line (≤ 60 chars, TOWER_DESIGN §10)", () => {
    for (const [k, v] of Object.entries(EXPLAIN)) if ("sub" in v) expect(v.sub.length, k).toBeLessThanOrEqual(60);
  });

  it("every InfoTip is at most two short sentences", () => {
    for (const [k, v] of Object.entries(EXPLAIN)) {
      expect(v.tip.length, k).toBeLessThanOrEqual(140);
      expect(v.tip.split(/[.!?](?:\s|$)/).filter((s) => s.trim().length > 1).length, k).toBeLessThanOrEqual(2);
    }
  });
});

describe("labels", () => {
  it("formats ranges without a tz shift", () => {
    expect(formatRange("2026-07-01", "2026-09-28")).toBe("Jul 1 – Sep 28, 2026");
    expect(formatRange("2026-08-03", "2026-08-09")).toBe("Aug 3 – 9, 2026");
    expect(formatRange("2025-12-29", "2026-01-02")).toBe("Dec 29, 2025 – Jan 2, 2026");
    expect(formatRange("2026-09-28", "2026-09-28")).toBe("Sep 28, 2026");
  });

  it("E13.13: persona labels lead with the D56 emoji (mirrors arc/slack/personas.py)", () => {
    expect(personaLabel("research")).toBe("🧠 Research");
    expect(personaLabel("scout")).toBe("🔭 Scout");
    expect(personaLabel("auditor")).toBe("🏦 Broker");
    expect(personaLabel("quant_pop")).toBe("Quant PoP");
    expect(withEmoji("risk.exit", "Risk (exit)")).toBe("🛡️ Risk (exit)");
    expect(withEmoji("monitor", "Monitor")).toBe("Monitor");
  });
});

describe("views", () => {
  it("shades the distance below the running peak", () => {
    const p = {
      equity: {
        empty: false,
        points: [
          { day: "2026-09-01", equity: 100 },
          { day: "2026-09-02", equity: 110 },
          { day: "2026-09-03", equity: 99 },
          { day: "2026-09-04", equity: 112 },
        ],
      },
    } as unknown as Performance;
    expect(equityView(p).map((d) => d.underwater)).toEqual([0, 0, -11, 0]);
    expect(equityView(p)[2]?.label).toBe("Sep 3");
  });

  it("links a breakdown row to the pre-filtered Trades list over the period", () => {
    const period = {
      first: "2026-07-01",
      last: "2026-09-28",
      slot_end: "2026-09-28",
      days: 90,
    };
    const row = {
      key: "SPY",
      label: "SPY",
      count: 3,
      wins: 2,
      pnl: 10,
      win_rate: 0.66,
      share: 0.2,
    };
    expect(tradesLink({ ...row, filter: { ticker: "SPY", stage: "closed" } }, period)).toBe(
      "/trades?ticker=SPY&stage=closed&date=custom&date_from=2026-07-01&date_to=2026-09-28",
    );
    expect(tradesLink({ ...row, filter: null }, period)).toBeNull();
  });
});
