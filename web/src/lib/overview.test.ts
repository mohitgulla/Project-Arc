import { describe, expect, it } from "vitest";

import {
  exitCaseText,
  exitPathRows,
  exitPathStripText,
  exitPathVisible,
  exitVerdictChip,
  exitWatchChip,
  mandatoryLabel,
  type PositionRow,
} from "./overview";

import {
  directionView,
  equityView,
  exitStatus,
  parseOverviewRange,
  marginSplitView,
  pnlSplit,
  proposalStage,
  greekRiskLabel,
  sortMovers,
  stripTone,
  structureLabel,
  violationCode,
  withBenchmarks,
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
  it("renders structure kinds in Title Case (D50; Slack stays sentence case)", () => {
    expect(structureLabel("vertical_debit")).toBe("Debit Vertical");
    expect(structureLabel("vertical_credit")).toBe("Credit Vertical");
    expect(structureLabel("long_call")).toBe("Long Call");
    expect(structureLabel("long_put")).toBe("Long Put");
    expect(structureLabel("iron_condor")).toBe("Iron Condor");
    expect(structureLabel("covered_call")).toBe("Covered Call");
    expect(structureLabel("cash_secured_put")).toBe("Cash-Secured Put");
    expect(structureLabel("some_new_kind")).toBe("Some New Kind");
    expect(structureLabel(null)).toBe("—");
  });

  it("direction label and colour", () => {
    expect(directionView("bullish")).toEqual({ label: "Bullish", className: "text-pos-text" });
    expect(directionView("bearish")).toEqual({ label: "Bearish", className: "text-neg-text" });
    expect(directionView("neutral")).toEqual({ label: "Neutral", className: "text-secondary" });
    expect(directionView(null)).toBeNull();
    expect(directionView(undefined)).toBeNull();
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

  it("movers rank by day change, best to worst, unknown last (D87)", () => {
    const out = sortMovers([{ change_today: 0.01 }, { change_today: null }, { change_today: -0.08 }, { change_today: 0.04 }]);
    expect(out.map((m) => m.change_today)).toEqual([0.04, 0.01, -0.08, null]);
  });

  it("greek advisory labels (D87)", () => {
    expect(greekRiskLabel("low")).toEqual({ text: "Risk Low", tone: "pos" });
    expect(greekRiskLabel("med")).toEqual({ text: "Risk Med", tone: "warn" });
    expect(greekRiskLabel("high")).toEqual({ text: "Risk High", tone: "neg" });
    expect(greekRiskLabel(null).text).toBe("Risk —");
  });

  it("benchmarks rebase onto the portfolio's start equity (D87)", () => {
    const t0 = "2026-09-28T13:30:00Z";
    const t1 = "2026-09-28T14:30:00Z";
    const series = [
      { t: t0, v: 100_000 },
      { t: t1, v: 101_000 },
    ];
    const e = {
      benchmarks: [
        { symbol: "SPY", start_value: "500", value: "505", value_at: t1, change_pct: 0.01,
          series: [{ t: t1, v: "505" }] },
      ],
    } as unknown as EquitySection;
    const out = withBenchmarks(e, series, 100_000);
    expect(out.lines.map((l) => [l.symbol, l.key, l.changePct])).toEqual([["SPY", "b_SPY", 0.01]]);
    expect(out.data.map((d) => d.b_SPY)).toEqual([undefined, 101_000]);
    expect(withBenchmarks(undefined, series, 100_000)).toEqual({ data: series, lines: [] });
  });

  it("realized/unrealized split uses magnitudes", () => {
    expect(pnlSplit(150, -228)).toEqual({ realized: 150, unrealized: 228 });
    expect(pnlSplit(null, null)).toEqual({ realized: 0, unrealized: 0 });
  });
});

describe("marginSplitView (D129)", () => {
  const base = {
    at: "2026-10-16T15:55:00-04:00",
    equity: "23540.75",
    cash: "20065.75",
    in_positions: "3475.00",
    cash_pct: 0.852,
    options_buying_power: "18000.00",
    buying_power_kind: "margin" as const,
    stock_buying_power: "80263.0",
    collateral: "2065.75",
    available: "18000.00",
  };

  it("is null for a cash-settled or unknown profile", () => {
    expect(marginSplitView({ ...base, buying_power_kind: "cash_settled" })).toBeNull();
    expect(marginSplitView({ ...base, buying_power_kind: null })).toBeNull();
  });

  it("reads the backend fields on margin", () => {
    const v = marginSplitView(base);
    expect(v).not.toBeNull();
    expect(v?.available).toBe(18000);
    expect(v?.collateral).toBe(2065.75);
    expect(v?.invested).toBe(3475);
    expect(v?.cash).toBe(20065.75);
    expect(v?.stockBuyingPower).toBe(80263);
    expect(v?.bar).toEqual({ available: 18000, collateral: 2065.75, invested: 3475 });
  });

  it("clamps a negative invested to 0 in the bar only", () => {
    const v = marginSplitView({ ...base, in_positions: "-1200.00" });
    expect(v?.invested).toBe(-1200);
    expect(v?.bar.invested).toBe(0);
  });

  it("falls back to cash when options buying power is unknown", () => {
    const v = marginSplitView({
      ...base,
      options_buying_power: null,
      collateral: null,
      available: "20065.75",
      stock_buying_power: null,
    });
    expect(v?.available).toBe(20065.75);
    expect(v?.collateral).toBe(0);
    expect(v?.stockBuyingPower).toBeNull();
  });
});

describe("E13.14 exit path", () => {
  const strip = { mode: "research", mandatory_pending: 1, cases_today: 2, closes_proposed_today: 1, holds_today: 1 } as const;
  const row = (over: Partial<PositionRow>) =>
    ({ exit_watch: null, exit_case: null, exit_review: null, mandatory_signal: null, ...over }) as PositionRow;

  it("shows on the Research path (deterministic = an old fixture)", () => {
    expect(exitPathVisible(strip)).toBe(true);
    expect(exitPathVisible({ ...strip, mode: "deterministic" })).toBe(false);
    expect(exitPathVisible(undefined)).toBe(false);
  });

  it("writes the strip with plurals", () => {
    expect(exitPathStripText(strip)).toBe("Research · 1 mandatory pending · 2 cases today · 1 close proposed · 1 hold");
    expect(exitPathStripText({ ...strip, mode: "research", cases_today: 1, holds_today: 0 })).toBe(
      "Research · 1 mandatory pending · 1 case today · 1 close proposed · 0 holds",
    );
  });

  it("tones the watch, case and verdict", () => {
    const w: NonNullable<PositionRow["exit_watch"]> = { as_of: "", action: "review", thesis_status: "weakened", evidence: [], reason: "" };
    expect(exitWatchChip(row({ exit_watch: w }))).toEqual({ text: "Review · Weakened", tone: "warn" });
    expect(exitWatchChip(row({ exit_watch: { ...w, action: "hold", thesis_status: "intact" } }))?.tone).toBe("neutral");
    expect(exitWatchChip(row({ exit_watch: { ...w, thesis_status: "broken" } }))?.tone).toBe("neg");
    expect(exitWatchChip(row({}))).toBeNull();
    const c: NonNullable<PositionRow["exit_case"]> = { as_of: "", triggers: [], recommendation: "close", remaining_ev_hold: -6.5, remaining_ev_managed: null, close_now_net: 74, stop_state: "off", rationale: "" };
    expect(exitCaseText(row({ exit_case: c }))).toBe("Close · EV −$6.50");
    expect(exitCaseText(row({ exit_case: { ...c, recommendation: "hold", remaining_ev_managed: 21 } }))).toBe("Hold · EV $21.00");
    expect(exitCaseText(row({ exit_case: { ...c, remaining_ev_hold: null } }))).toBe("Close");
    const v: NonNullable<PositionRow["exit_review"]> = { as_of: "", verdict: "close", reason_code: "thesis_broken", reason: "", unavailable: false };
    expect(exitVerdictChip(row({ exit_review: v }))).toEqual({ text: "Close", tone: "neg" });
    expect(exitVerdictChip(row({ exit_review: { ...v, verdict: "hold" } }))).toEqual({ text: "Hold", tone: "pos" });
    expect(exitVerdictChip(row({ exit_review: { ...v, verdict: "hold", unavailable: true } }))?.text).toBe("Unavailable");
  });

  it("labels mandatory signals and lists rows with anything to show", () => {
    expect(mandatoryLabel("dte_exit")).toBe("DTE exit");
    expect(mandatoryLabel("stop")).toBe("stop");
    expect(mandatoryLabel(null)).toBeNull();
    expect(exitPathRows([row({}), row({ mandatory_signal: "stop" })])).toHaveLength(1);
  });
});
