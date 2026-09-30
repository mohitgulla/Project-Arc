import { describe, expect, it } from "vitest";

import {
  activeFilterCount,
  apiQuery,
  humanize,
  pageCount,
  stageTone,
  stepperFor,
  tradeQuery,
} from "./trades";

const q = (s: string) => tradeQuery(new URLSearchParams(s));

describe("tradeQuery", () => {
  it("defaults", () => {
    expect(q("")).toEqual({ query: {}, page: 1, size: 50, sort: "time", dir: "desc" });
  });
  it("reads filters, sort and paging from the URL", () => {
    const t = q("ticker=AMD,SPY&stage=open&min_pop=0.5&sort=net_ev&dir=asc&page=3&size=25&junk=1");
    expect(t.query).toEqual({ ticker: "AMD,SPY", stage: "open", min_pop: "0.5" });
    expect([t.sort, t.dir, t.page, t.size]).toEqual(["net_ev", "asc", 3, 25]);
    expect(apiQuery(t)).toEqual({ ticker: "AMD,SPY", stage: "open", min_pop: "0.5", page: 3, size: 25, sort: "net_ev", dir: "asc" });
  });
  it("falls back on bad values", () => {
    const t = q("sort=bogus&dir=up&page=0&size=999");
    expect([t.sort, t.dir, t.page, t.size]).toEqual(["time", "desc", 1, 50]);
  });
  it("drops the custom range unless date=custom", () => {
    expect(q("date=7d&date_from=2026-09-01").query).toEqual({ date: "7d" });
    expect(q("date=custom&date_from=2026-09-01&date_to=2026-09-05").query).toEqual({
      date: "custom",
      date_from: "2026-09-01",
      date_to: "2026-09-05",
    });
  });
  it("maps the Overview's since=today link", () => {
    expect(q("since=today").query).toEqual({ date: "today" });
  });
});

describe("helpers", () => {
  it("pageCount", () => {
    expect(pageCount(0, 50)).toBe(1);
    expect(pageCount(101, 50)).toBe(3);
  });
  it("activeFilterCount counts each multi value", () => {
    expect(activeFilterCount(new URLSearchParams("ticker=A,B&date=custom&date_from=x&q=z"))).toBe(4);
  });
  it("stepperFor marks the failed stage", () => {
    expect(stepperFor("gate_fail")).toEqual({ reached: "gate", failedAt: "gate" });
    expect(stepperFor("expired")).toEqual({ reached: "approval", failedAt: "approval" });
    expect(stepperFor("cancelled")).toEqual({ reached: "execution", failedAt: "execution" });
    expect(stepperFor("closed")).toEqual({ reached: "closed" });
  });
  it("stageTone and humanize", () => {
    expect(stageTone("gate_fail")).toBe("neg");
    expect(stageTone("closed")).toBe("pos");
    expect(stageTone("approved")).toBe("neutral");
    expect(humanize("profit_target")).toBe("Profit target");
    expect(humanize("vertical_debit")).toBe("Vertical debit");
    expect(humanize(null)).toBe("—");
  });
});
