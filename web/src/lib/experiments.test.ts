import { describe, expect, it } from "vitest";

import {
  armRows,
  breakdownView,
  ciText,
  ciTone,
  cumulativeView,
  curveView,
  deltaText,
  pct,
  pText,
  ratioText,
  secondaryText,
  sessionsText,
  statusText,
  type ExperimentDetail,
  type ExperimentReport,
  type ExperimentRow,
} from "./experiments";

const ROW = {
  experiment_id: "XP-2",
  title: "TP 40%",
  status: "running",
  reason: null,
  area: "exits",
  kind: "ab",
  min_sessions: 20,
  max_sessions: 60,
  sessions: 14,
  primary_mean: 0.0008,
  primary_ci_lo: -0.0003,
  primary_ci_hi: 0.0019,
  ci_level: 0.95,
  secondary: "ok",
  sortino_control: 1.1,
  sortino_treatment: 1.4,
  verdict: "continue",
  verdict_reason: "14/20-60",
  evaluated_at: "2026-10-23T16:45:00-04:00",
  as_of_day: "2026-10-23",
  line: "[Experiments] XP-2 • exits • day 14/20–60 • Δ +0.08%/day [−0.03, +0.19] • Sortino ok",
} as unknown as ExperimentRow;

describe("experiment formatting (same text as the Slack line)", () => {
  it("formats percentages with a real minus", () => {
    expect(pct(0.0008)).toBe("+0.08%");
    expect(pct(-0.0003)).toBe("−0.03%");
    expect(pct(null)).toBe("n/a");
    expect(pct(0.0123, 3, { sign: false })).toBe("1.230%");
  });

  it("renders delta + CI exactly like the Slack line", () => {
    expect(`Δ ${deltaText(ROW)} ${ciText(ROW)}`).toBe("Δ +0.08%/day [−0.03, +0.19]");
    expect(ROW.line).toContain(`Δ ${deltaText(ROW)} ${ciText(ROW)}`);
    expect(sessionsText(ROW)).toBe("14 / 20–60");
    expect(secondaryText(ROW)).toBe("Sortino ok");
    expect(ciTone(ROW)).toBe("neutral");
    expect(ciTone({ primary_ci_lo: 0.001, primary_ci_hi: 0.002 })).toBe("pos");
    expect(ciTone({ primary_ci_lo: -0.002, primary_ci_hi: -0.001 })).toBe("neg");
  });

  it("handles an experiment with no report yet", () => {
    const draft = { ...ROW, sessions: null, primary_mean: null, primary_ci_lo: null, primary_ci_hi: null, secondary: null };
    expect(deltaText(draft)).toBe("n/a");
    expect(ciText(draft)).toBe("—");
    expect(sessionsText(draft)).toBe("— / 20–60");
    expect(secondaryText(draft)).toBe("—");
    expect(statusText({ status: "stopped", reason: "win" })).toBe("stopped (win)");
  });
});

describe("detail views", () => {
  it("maps curves and the cumulative band to percent", () => {
    const d = {
      curves: [
        { day: null, control: 100000, treatment: 100000 },
        { day: "2026-10-05", control: 100100, treatment: 100250 },
      ],
      cumulative: [
        { day: "2026-10-05", n: 1, cum_d: 0.0015, lo: null, hi: null },
        { day: "2026-10-06", n: 2, cum_d: 0.002, lo: -0.001, hi: 0.005 },
      ],
    } as unknown as ExperimentDetail;
    expect(curveView(d)).toEqual([
      { label: "t0", control: 100000, treatment: 100000 },
      { label: "Oct 5", control: 100100, treatment: 100250 },
    ]);
    const cum = cumulativeView(d);
    expect(cum[0]).toEqual({ label: "Oct 5", cum: 0.15, band: null });
    expect(cum[1]?.band?.[0]).toBeCloseTo(-0.1);
    expect(cum[1]?.band?.[1]).toBeCloseTo(0.5);
  });

  it("builds the per-arm table and pivots breakdowns", () => {
    const r = {
      arms: [
        {
          arm: "control", arm_id: null, sessions: 14, total_pnl: 812.5, max_drawdown: 0.0123,
          worst_day: -0.008, orders: 3, filled_executions: 3, executions: 3, mean_slippage_bps: null,
        },
        {
          arm: "treatment", arm_id: "XP-2:treatment", sessions: 14, total_pnl: 1612.5, max_drawdown: 0,
          worst_day: null, orders: 8, filled_executions: 4, executions: 4, mean_slippage_bps: 4.25,
        },
      ],
      breakdowns: [
        { by: "regime", key: "risk_on", arm: "control", trades: 2, realised_pnl: 50 },
        { by: "regime", key: "risk_on", arm: "treatment", trades: 3, realised_pnl: 90 },
        { by: "structure_kind", key: "vertical_debit", arm: "treatment", trades: 1, realised_pnl: -10 },
      ],
    } as unknown as ExperimentReport;
    const rows = armRows(r);
    expect(rows[0]).toMatchObject({ arm: "Control", maxDrawdown: "−1.23%", worstDay: "−0.80%", orders: "3", fills: "3/3", slippage: "n/a" });
    expect(rows[1]).toMatchObject({ arm: "Treatment", maxDrawdown: "0.00%", worstDay: "n/a", orders: "8", slippage: "4.3 bps" });
    expect(breakdownView(r)).toEqual([
      { by: "regime", key: "risk_on", control: { trades: 2, pnl: 50 }, treatment: { trades: 3, pnl: 90 } },
      { by: "structure_kind", key: "vertical_debit", control: null, treatment: { trades: 1, pnl: -10 } },
    ]);
    expect(armRows(null)).toEqual([]);
  });
});

describe("p-value and Sortino text (owner line format)", () => {
  it("formats p like the Slack line", () => {
    expect(pText(undefined)).toBe("n/a");
    expect(pText(0.0004)).toBe("<0.001");
    expect(pText(0.004)).toBe("0.004");
    expect(pText(0.214)).toBe("0.21");
  });
  it("signs Sortino deltas with a real minus", () => {
    expect(ratioText(0.35, true)).toBe("+0.35");
    expect(ratioText(-0.1, true)).toBe("−0.10");
    expect(ratioText(null)).toBe("n/a");
  });
});
