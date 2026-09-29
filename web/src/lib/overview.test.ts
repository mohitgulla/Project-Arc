import { describe, expect, it } from "vitest";

import {
  equityView,
  exitStatus,
  parseOverviewRange,
  pnlSplit,
  proposalStage,
  sortMovers,
  stripTone,
  structureLabel,
  violationCode,
  type EquitySection,
  type Overview,
} from "./overview";

const intraday: EquitySection = {
  range: "1D",
  value: "101133.98",
  value_at: "2026-09-28T15:38:00-04:00",
  source: "intraday",
  start_value: "100861.21",
  start_at: "2026-09-25T16:00:00-04:00",
  start_label: "prev close",
  change: "272.77",
  change_pct: 0.0027,
  series_source: "intraday",
  series: [
    { t: "2026-09-28T13:23:00-04:00", v: "100900.00" },
    { t: "2026-09-28T13:18:00-04:00", v: "100861.21" }, // out of order: sorted by time
    { t: "2026-09-28T15:38:00-04:00", v: "101133.98" },
  ],
};

const daily: EquitySection = {
  ...intraday,
  range: "1M",
  source: "intraday",
  start_value: "100090.00",
  start_label: "09-14",
  series_source: "daily",
  series: [
    { t: "2026-09-14T16:00:00-04:00", v: "100090.00" },
    { t: "2026-09-25T16:00:00-04:00", v: "100861.21" },
    { t: "2026-09-28T15:38:00-04:00", v: "101133.98" },
  ],
};

describe("range -> series selection", () => {
  it("parses only the ranges the API accepts, default 1D", () => {
    expect(parseOverviewRange("1W")).toBe("1W");
    expect(parseOverviewRange("ALL")).toBe("ALL");
    expect(parseOverviewRange("1Y")).toBe("1D");
    expect(parseOverviewRange(null)).toBe("1D");
  });

  it("1D plots the intraday monitor marks, sorted, against the prior close", () => {
    const v = equityView(intraday, "1D");
    expect(v.source).toBe("intraday");
    expect(v.series.map((p) => p.v)).toEqual([100861.21, 100900, 101133.98]);
    expect(v.reference).toBe(100861.21);
    expect(v.change).toBeCloseTo(272.77);
    expect(v.changePct).toBeCloseTo(0.0027);
  });

  it("other ranges plot the daily series with the range start as reference", () => {
    const v = equityView(daily, "1M");
    expect(v.source).toBe("daily");
    expect(v.series).toHaveLength(3);
    expect(v.reference).toBe(100090);
  });

  it("does not plot a response for another range (poll raced a range click)", () => {
    const v = equityView(intraday, "1M");
    expect(v.series).toEqual([]);
    expect(v.source).toBe("daily");
    expect(v.reference).toBeUndefined();
    expect(equityView(undefined, "1D").source).toBe("intraday");
  });

  it("drops non-numeric points", () => {
    const v = equityView({ ...intraday, series: [{ t: intraday.value_at!, v: "x" }] }, "1D");
    expect(v.series).toEqual([]);
  });
});

describe("labels", () => {
  it("renders structure kinds in sentence case", () => {
    expect(structureLabel("vertical_debit")).toBe("Debit vertical");
    expect(structureLabel("long_call")).toBe("Long call");
    expect(structureLabel("some_new_kind")).toBe("Some new kind");
    expect(structureLabel(null)).toBe("—");
  });

  it("exit status", () => {
    expect(exitStatus({ exit_pending: true, exit_reason: "profit_target" })).toBe("pending · profit target");
    expect(exitStatus({ exit_pending: true, exit_reason: null })).toBe("pending");
    expect(exitStatus({ exit_pending: false, exit_reason: "stop" })).toBe("stop");
    expect(exitStatus({ exit_pending: false, exit_reason: null })).toBe("—");
  });

  it("violation code is the text before the colon", () => {
    expect(violationCode("max_alloc: max loss $5,400 > $5,000")).toBe("max_alloc");
    expect(violationCode("halted")).toBe("halted");
    expect(violationCode(undefined)).toBeNull();
  });
});

describe("proposal lifecycle", () => {
  const p = (gate_passed: boolean | null, approval: string | null = null, execution: string | null = null) => ({
    gate_passed,
    approval,
    execution,
  });
  it.each([
    [p(null), "proposed", undefined],
    [p(false, "not_actionable"), "gate", "gate"],
    [p(true), "gate", undefined],
    [p(true, "pending"), "gate", undefined],
    [p(true, "approved"), "approval", undefined],
    [p(true, "rejected"), "approval", "approval"],
    [p(true, "expired"), "approval", "approval"],
    [p(true, "approved", "working"), "execution", undefined],
    [p(true, "approved", "cancelled"), "execution", "execution"],
    [p(true, "approved", "filled"), "filled", undefined],
  ])("%o -> %s", (row, reached, failedAt) => {
    const s = proposalStage(row);
    expect(s.reached).toBe(reached);
    expect(s.failedAt).toBe(failedAt);
  });
});

describe("strip, movers, split", () => {
  const base = { halted: false, alerts: [] } as unknown as Overview["status"];
  it("strip tone: halt > alerts > ok", () => {
    expect(stripTone({ status: { ...base, halted: true } })).toBe("neg");
    expect(stripTone({ status: { ...base, alerts: [{ kind: "k", key: "k", message: "m", opened_at: null }] } })).toBe("warn");
    expect(stripTone({ status: base })).toBe("ok");
  });

  it("movers sort by absolute day change, unknown last", () => {
    const out = sortMovers([{ change_today: 0.01 }, { change_today: null }, { change_today: -0.08 }]);
    expect(out.map((m) => m.change_today)).toEqual([-0.08, 0.01, null]);
  });

  it("realized/unrealized split uses magnitudes", () => {
    expect(pnlSplit(150, -228)).toEqual({ realized: 150, unrealized: 228 });
    expect(pnlSplit(null, null)).toEqual({ realized: 0, unrealized: 0 });
  });
});
