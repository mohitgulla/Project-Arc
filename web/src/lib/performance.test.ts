import { describe, expect, it } from "vitest";

import {
  apiQuery,
  comparisonLine,
  equityView,
  formatRange,
  perfQuery,
  tradesLink,
  type Performance,
} from "./performance";

const P = (s: string) => perfQuery(new URLSearchParams(s));

describe("perfQuery", () => {
  it("defaults to 90d vs previous period, tests excluded", () => {
    expect(P("")).toEqual({
      preset: "90d",
      compare: "prev",
      include_tests: false,
      by: "ticker",
      shadow: false,
    });
    expect(apiQuery(P(""))).toEqual({ preset: "90d", compare: "prev" });
  });

  it("reads every control from the URL", () => {
    const q = P("preset=ytd&compare=yoy&include_tests=true&by=regime&shadow=true");
    expect(q).toMatchObject({
      preset: "ytd",
      compare: "yoy",
      include_tests: true,
      by: "regime",
      shadow: true,
    });
    expect(apiQuery(q)).toEqual({
      preset: "ytd",
      compare: "yoy",
      include_tests: "true",
    });
  });

  it("falls back on junk and on a custom range without a start", () => {
    expect(P("preset=nope&compare=x&by=y")).toMatchObject({
      preset: "90d",
      compare: "prev",
      by: "ticker",
    });
    expect(P("preset=custom").preset).toBe("90d");
    expect(P("preset=custom&from=2026-08-01&to=bad")).toMatchObject({
      preset: "custom",
      from: "2026-08-01",
    });
    expect(apiQuery(P("preset=custom&from=2026-08-01&to=2026-08-31"))).toEqual({
      preset: "custom",
      compare: "prev",
      from: "2026-08-01",
      to: "2026-08-31",
    });
  });
});

describe("labels", () => {
  it("formats ranges without a tz shift", () => {
    expect(formatRange("2026-07-01", "2026-09-28")).toBe("Jul 1 – Sep 28, 2026");
    expect(formatRange("2026-08-03", "2026-08-09")).toBe("Aug 3 – 9, 2026");
    expect(formatRange("2025-12-29", "2026-01-02")).toBe("Dec 29, 2025 – Jan 2, 2026");
    expect(formatRange("2026-09-28", "2026-09-28")).toBe("Sep 28, 2026");
  });

  it("writes the comparison line only with a value and a period", () => {
    const period = {
      first: "2026-04-02",
      last: "2026-06-30",
      slot_end: "2026-06-30",
      days: 90,
    };
    expect(comparisonLine(-570.53, period)).toBe("vs -$570.53 in Apr 2 – Jun 30, 2026");
    expect(comparisonLine(null, period)).toBeNull();
    expect(comparisonLine(12, null)).toBeNull();
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
