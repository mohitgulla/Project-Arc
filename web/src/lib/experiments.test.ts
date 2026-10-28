import { describe, expect, it } from "vitest";

import {
  ACCOUNT_NAME,
  CONTROL_COLOR,
  CONTROL_NAME,
  armColor,
  armLabel,
  armPlanRows,
  armRows,
  armShort,
  breakdownView,
  ciText,
  ciTone,
  dailyDiffView,
  deltaText,
  detectableRows,
  duelRows,
  duelWinner,
  equityView,
  forestDomain,
  groupExperiments,
  harnessRows,
  kindLabel,
  leaderRows,
  pairSentence,
  pct,
  pText,
  productionGapRows,
  productionView,
  ratioText,
  refName,
  regimeRows,
  resultRows,
  resultSentences,
  sessionsText,
  sessionsView,
  spanDomain,
  startBookRows,
  statusPill,
  statusText,
  variantRows,
  verdictCounts,
  verdictPill,
  type ExperimentDetail,
  type ExperimentReport,
  type ExperimentRow,
  type RegimePanel,
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
  variants: 1,
  headline_arm: "treatment",
  arm_verdicts: { treatment: "continue" },
} as unknown as ExperimentRow;

/** The stored XP-10 report (archived pre-D80 store, 2026-10-09), trimmed to what the page reads. */
const XP10 = {
  report_version: 1,
  experiment_id: "XP-10",
  kind: "aa",
  sessions: 3,
  min_sessions: 10,
  max_sessions: 10,
  t0_equity: 98354.85,
  verdict: "continue",
  series: [
    { day: "2026-10-07", control_pnl: -130.39, treatment_pnl: -130, d: 3.965e-6 },
    { day: "2026-10-08", control_pnl: -65.57, treatment_pnl: -1840, d: -0.01804 },
    { day: "2026-10-09", control_pnl: -616.94, treatment_pnl: 300, d: 0.009323 },
  ],
  primary: { mean: -0.0029048, ci: { lo: -0.1165173, hi: 0.1107077, level: 0.95 }, p_value: 1.0 },
  secondary: { sortino_control: -11.7519, sortino_treatment: -8.2977, diff_ci: { lo: -0.91, hi: 47.31, level: 0.9 }, margin: null, p_value: 0.29 },
  arms: [
    { arm: "control", arm_id: null, sessions: 3, total_pnl: -812.9, max_drawdown: 0.008265, worst_day: -0.006285, orders: 44, filled_executions: 10, executions: 15, mean_slippage_bps: 42.989 },
    { arm: "treatment", arm_id: "XP-10:treatment", sessions: 3, total_pnl: -1670, max_drawdown: 0.02003, worst_day: -0.018733, orders: 25, filled_executions: 4, executions: 8, mean_slippage_bps: null },
  ],
  calibration: {
    sigma: 0.0139119,
    mde_always_valid: { "10": 0.0170558, "20": 0.012139, "40": 0.0087269, "60": 0.0072118 },
    fill_rate_gap: -0.16667,
    slippage_gap_bps: null,
    paired_chains: 110,
    divergent_chains: 101,
    llm_divergence_rate: 0.91818,
  },
  breakdowns: [
    { by: "regime", key: "transitional", arm: "control", trades: 3, realised_pnl: -195 },
    { by: "structure_kind", key: "long_call", arm: "control", trades: 1, realised_pnl: -75 },
    { by: "structure_kind", key: "vertical_debit", arm: "control", trades: 2, realised_pnl: -120 },
  ],
} as unknown as ExperimentReport;

/** A K=3 report shaped like tests/test_experiment_holm.py::test_k3_report_shape_and_verdicts. */
const K3 = {
  report_version: 2,
  experiment_id: "XP-2",
  kind: "ab",
  sessions: 30,
  min_sessions: 20,
  max_sessions: 40,
  t0_equity: 100000,
  verdict: "continue",
  headline_arm: "t1",
  series: [],
  primary: { mean: 0.00389, ci: { lo: 0.00299, hi: 0.00479, level: 0.95 }, p_value: 1e-33 },
  secondary: { sortino_control: -6.68, sortino_treatment: 63.04, margin: 0.2, p_value: 0.01 },
  arms: [
    { arm: "control", total_pnl: -3127, max_drawdown: 0.0331, cum_pnl_pct: -0.03127, sortino: -6.68, rank: 2, orders: 0, filled_executions: 0, executions: 0 },
    { arm: "t1", total_pnl: 8859, max_drawdown: 0.0032, cum_pnl_pct: 0.0886, sortino: 63.04, rank: 1, verdict: "win", orders: 0, filled_executions: 0, executions: 0 },
    { arm: "t2", total_pnl: -15327, max_drawdown: 0.1533, cum_pnl_pct: -0.1533, sortino: -14.03, rank: 4, verdict: "loss", orders: 0, filled_executions: 0, executions: 0 },
    { arm: "t3", total_pnl: -3428, max_drawdown: 0.0379, cum_pnl_pct: -0.03428, sortino: -7.13, rank: 3, verdict: "continue", orders: 0, filled_executions: 0, executions: 0 },
  ],
  pairwise: [
    { kind: "control_vs_treatment", a: "control", b: "t1", n: 20, mean: 0.00389, ci: { lo: 0.00299, hi: 0.00479, level: 0.95 }, holm_p: 3e-37, verdict: "win", series: [{ day: "2026-10-05", control_pnl: 10, treatment_pnl: 400, d: 0.0039 }] },
    { kind: "control_vs_treatment", a: "control", b: "t2", n: 20, mean: -0.00408, ci: { lo: -0.00492, hi: -0.00323, level: 0.95 }, holm_p: 3e-45, verdict: "loss", series: [{ day: "2026-10-05", control_pnl: 10, treatment_pnl: -390, d: -0.004 }] },
    { kind: "control_vs_treatment", a: "control", b: "t3", n: 30, mean: -0.0001, ci: { lo: -0.00073, hi: 0.00053, level: 0.95 }, holm_p: 1, verdict: "continue", series: [{ day: "2026-10-05", control_pnl: 10, treatment_pnl: 5, d: 0 }] },
    { kind: "treatment_vs_treatment", a: "t1", b: "t2", n: 30, mean: -0.00806, ci: { lo: -0.00879, hi: -0.00733, level: 0.95 }, descriptive: true, verdict: null },
  ],
  omnibus: { k: 3, p_value: 3e-45, differs: true },
  calibration: { mde_always_valid: { "20": 0.002 } },
  breakdowns: [],
} as unknown as ExperimentReport;

describe("number text (same as the Slack line)", () => {
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
    expect(ciTone(ROW)).toBe("neutral");
    expect(ciTone({ primary_ci_lo: 0.001, primary_ci_hi: 0.002 })).toBe("pos");
    expect(ciTone({ primary_ci_lo: -0.002, primary_ci_hi: -0.001 })).toBe("neg");
  });

  it("formats p and Sortino like the Slack line", () => {
    expect(pText(undefined)).toBe("n/a");
    expect(pText(0.0004)).toBe("<0.001");
    expect(pText(0.004)).toBe("0.004");
    expect(pText(0.214)).toBe("0.21");
    expect(ratioText(0.35, true)).toBe("+0.35");
    expect(ratioText(-0.1, true)).toBe("−0.10");
    expect(ratioText(null)).toBe("n/a");
  });
});

describe("names, status and verdict pills (D92/D95)", () => {
  it("names the production arm Production and arms by treatment", () => {
    expect(armShort("control")).toBe(ACCOUNT_NAME);
    expect(armShort("treatment")).toBe("T1");
    expect(armShort("t3")).toBe("T3");
    expect(armLabel("t2", "XP-2")).toBe("XP-2 · T2");
    expect(armLabel("control", "XP-2")).toBe("Production");
    expect(armColor("control", ["control", "t1"])).toBe("var(--accent)");
    expect(armColor("t2", ["control", "t1", "t2"])).not.toBe(armColor("t1", ["control", "t1", "t2"]));
  });

  it("Title Cases status and verdicts", () => {
    expect(statusPill({ status: "running", reason: null })).toEqual({ label: "Running", tone: "accent" });
    expect(statusPill({ status: "stopped", reason: "owner" })).toEqual({ label: "Stopped · Owner", tone: "neutral" });
    expect(statusPill({ status: "stopped", reason: "win" }).tone).toBe("pos");
    expect(statusText({ status: "stopped", reason: "win" })).toBe("stopped (win)");
    expect(verdictPill("continue").label).toBe("No Call Yet");
    expect(verdictPill("win")).toEqual({ label: "Win", tone: "pos" });
    expect(verdictPill(null).label).toBe("Not Evaluated");
    expect(kindLabel({ kind: "aa", variants: 1 })).toBe("A/A");
    expect(kindLabel({ kind: "ab", variants: 3 })).toBe("A/B · 3 Variants");
  });

  it("counts arm verdicts in a fixed order", () => {
    const c = verdictCounts({ arm_verdicts: { t3: "continue", t1: "win", t2: "loss" } });
    expect(c.map((x) => `${x.n} ${x.label}`)).toEqual(["1 Win", "1 Loss", "1 No Call Yet"]);
  });

  it("groups active before finished", () => {
    const g = groupExperiments([
      { ...ROW, experiment_id: "XP-10", status: "stopped" },
      ROW,
      { ...ROW, experiment_id: "XP-3", status: "draft" },
    ] as ExperimentRow[]);
    expect(g.map((x) => [x.title, x.rows.map((r) => r.experiment_id)])).toEqual([
      ["Active", ["XP-2", "XP-3"]],
      ["Finished", ["XP-10"]],
    ]);
  });

  it("describes session progress", () => {
    const v = sessionsView(ROW);
    expect(v.fraction).toBeCloseTo(14 / 60);
    expect(v.minFraction).toBeCloseTo(20 / 60);
    expect(v.caption).toBe("Day 14 · earliest call on day 20");
    expect(sessionsView({ ...ROW, sessions: 3, min_sessions: 10, max_sessions: 10, status: "stopped" })).toMatchObject({
      minFraction: null,
      caption: "Ended on day 3 of 10",
    });
    expect(sessionsView({ ...ROW, sessions: null }).caption).toBe("Waiting for the first EOD evaluation");
  });
});

describe("result: forest rows and sentences", () => {
  it("K=1 gives one row from the report's primary CI", () => {
    const rows = resultRows(XP10);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ label: "T1", tone: "neutral", right: "p 1.00" });
    expect(rows[0]?.lo).toBeCloseTo(-11.65, 1);
    expect(forestDomain(rows)).toBe(1); // 3× the 0.29% estimate, not the ±11% range
  });

  it("K=3 gives one Holm row per variant, toned by its CI", () => {
    const rows = resultRows(K3);
    expect(rows.map((r) => [r.label, r.tone, r.right])).toEqual([
      ["T1", "pos", "Holm p <0.001"],
      ["T2", "neg", "Holm p <0.001"],
      ["T3", "neutral", "Holm p 1.00"],
    ]);
    expect(variantRows(K3).map((r) => r.label)).toEqual(["T2 − T1"]);
  });

  it("writes the deterministic sentence for XP-10 (too early)", () => {
    expect(resultSentences({ report: XP10 })).toEqual([
      "Too early to tell: after 3 sessions the range still spans ±11.36%/day. At 10 sessions it can detect a ±1.71%/day gap.",
    ]);
  });

  it("writes one sentence per variant for K=3", () => {
    expect(resultSentences({ report: K3 })).toEqual([
      "T1 beats Production by 0.39%/day; the whole range is above 0.",
      "T2 trails Production by 0.41%/day; the whole range is below 0.",
      "T3: no call yet: the range −0.07% to +0.05% a day still includes 0.",
    ]);
  });

  it("covers the remaining sentence rules", () => {
    const base = { arm: "T1", mean: 0.001, lo: 0.0005, hi: 0.0015, sessions: 5, minSessions: 20, mdeAtMin: null, aa: false };
    expect(pairSentence({ ...base, verdict: "continue" })).toContain("ahead by 0.10%/day so far");
    expect(pairSentence({ ...base, mean: -0.001, lo: -0.0015, hi: -0.0005, verdict: "continue" })).toContain("behind by");
    expect(pairSentence({ ...base, verdict: "futility" })).toContain("no meaningful difference");
    expect(pairSentence({ ...base, verdict: "invalid", aa: true })).toContain("A/A check failed");
    expect(pairSentence({ ...base, mean: null, verdict: "continue" })).toBe("T1: not enough paired sessions for a range yet.");
    expect(resultSentences({ report: null })[0]).toContain("No evaluation yet");
  });

  it("lists the detectable effect by session count", () => {
    const rows = detectableRows(XP10);
    expect(rows.map((r) => [r.label, r.right])).toEqual([
      ["Now (day 3)", "±11.36%"],
      ["Day 10", "±1.71%"],
      ["Day 20", "±1.21%"],
      ["Day 40", "±0.87%"],
      ["Day 60", "±0.72%"],
    ]);
    expect(spanDomain(rows.slice(1))).toBe(2); // the MDE rows fit; "Now" (±11%) gets edge arrows
  });
});

describe("charts and tables", () => {
  it("v1 equity uses the served curves; v2 rebuilds every arm from the pairs", () => {
    const d = {
      report: XP10,
      curves: [
        { day: null, control: 98354.85, treatment: 98354.85 },
        { day: "2026-10-07", control: 98224.46, treatment: 98224.85 },
      ],
    } as unknown as ExperimentDetail;
    expect(equityView(d)).toEqual({
      arms: ["control", "treatment"],
      points: [
        { label: "t0", control: 98354.85, treatment: 98354.85 },
        { label: "Oct 7", control: 98224.46, treatment: 98224.85 },
      ],
    });
    const v2 = equityView({ report: K3, curves: [] } as unknown as ExperimentDetail);
    expect(v2.arms).toEqual(["control", "t1", "t2", "t3"]);
    expect(v2.points[1]).toEqual({ label: "Oct 5", control: 100010, t1: 100400, t2: 99610, t3: 100005 });
  });

  it("daily difference bars + running total in percent", () => {
    const v = dailyDiffView(XP10);
    expect(v.days).toBe(3);
    expect(v.ahead).toBe(2);
    expect(v.total).toBeCloseTo(-0.8713, 3);
    expect(v.bars[1]?.v).toBeCloseTo(-1.804, 3);
  });

  it("duel rows mark the better side", () => {
    const rows = duelRows(XP10);
    expect(rows.map((r) => [r.label, r.aText, r.bText, duelWinner(r)])).toEqual([
      ["Sortino", "−11.75", "−8.30", "b"],
      ["Total P&L", "-$812.90", "-$1,670.00", "a"],
      ["Max Drawdown", "−0.83%", "−2.00%", "a"],
      ["Worst Day", "−0.63%", "−1.87%", "a"],
    ]);
  });

  it("leaderboard ranks every arm; Production carries no verdict", () => {
    const rows = leaderRows(K3);
    expect(rows.map((r) => [r.arm, r.rank, r.cumText, r.verdict?.label ?? null])).toEqual([
      ["t1", 1, "+8.86%", "Win"],
      ["control", 2, "−3.13%", null],
      ["t3", 3, "−3.43%", "No Call Yet"],
      ["t2", 4, "−15.33%", "Loss"],
    ]);
  });

  it("execution and harness rows", () => {
    expect(armRows(XP10)).toEqual([
      { arm: "control", orders: "44", fillRate: "67%", fills: "10/15", slippage: "43.0 bps" },
      { arm: "treatment", orders: "25", fillRate: "50%", fills: "4/8", slippage: "n/a" },
    ]);
    expect(harnessRows(XP10)).toEqual([
      { label: "Decisions That Differed", value: "92%", hint: "101 of 110 chains" },
      { label: "Fill-Rate Gap", value: "−16.7 pts" },
      { label: "Daily Noise (σ)", value: "1.39%" },
    ]);
    expect(armRows(null)).toEqual([]);
  });

  it("breakdowns pivot per arm, structures first with D50 labels", () => {
    expect(breakdownView(XP10).map((b) => [b.by, b.label, b.cells.control?.trades])).toEqual([
      ["structure_kind", "Long Call", 1],
      ["structure_kind", "Debit Vertical", 2],
      ["regime", "Transitional", 3],
    ]);
  });
});

describe("arm plans (E13.12)", () => {
  it("lists each arm's fork step and own personas, sorted by arm", () => {
    const d = {
      running: {
        arm_plans: {
          treatment: { fork_step: "research", arm_personas: ["scout"], arm_jobs: ["monitor", "broker"], shared_kinds: [], own_producers: ["scout"] },
          shadow_control: { fork_step: "quant.propose", arm_personas: [], arm_jobs: ["monitor"], shared_kinds: [], own_producers: [] },
        },
      },
    } as unknown as ExperimentDetail;
    expect(armPlanRows(d)).toEqual([
      { arm: "Shadow Control", forkStep: "quant.propose", personas: "None (Production's)", jobs: "monitor" },
      { arm: "T1", forkStep: "research", personas: "Scout", jobs: "monitor, broker" },
    ]);
  });

  it("is empty before t0 and for pre-E13.12 runs", () => {
    expect(armPlanRows({ running: null } as unknown as ExperimentDetail)).toEqual([]);
    expect(armPlanRows(null)).toEqual([]);
  });
});

// D108 (E25.6): spec v3 naming (Production / Control / T1..T3) and the Production panel.
const V3 = {
  reference: "control",
  t0_equity: 100_000,
  production: {
    cum_p_pnl: -7289.19,
    cum_d_p_c: -0.0416,
    cum_start_book: 0.00435,
    cum_residual: -0.04598,
    ramped_from: null,
    sessions: [{}, {}, {}],
    start_book: {
      ids: ["sb-1", "sb-2"],
      close_dates: { "sb-1": null, "sb-2": "2026-10-09" },
      days: [{ day: "2026-11-13", open: 1, closed: 1, pnl: 15, cum_pnl: 435 }],
    },
    compare: [
      { arm: "control", basis: "total", n: 30, mean: 0.0014, ci: { lo: -0.01, hi: 0.013, estimate: 0.0014, level: 0.95 } },
      { arm: "t1", basis: "total", n: 30, mean: 0.0054, ci: { lo: -0.0063, hi: 0.0171, estimate: 0.0054, level: 0.95 } },
      { arm: "t1", basis: "ex_start_book", n: 30, mean: 0.0055, ci: { lo: -0.006, hi: 0.017, estimate: 0.0055, level: 0.95 } },
      { arm: "t1", basis: "post_ramp", n: 0, mean: null, ci: null },
    ],
  },
} as unknown as ExperimentReport;

describe("D108 naming: Production vs a v3 Control arm", () => {
  it("names control by the report's reference", () => {
    expect(ACCOUNT_NAME).toBe("Production");
    expect(refName(V3)).toBe(CONTROL_NAME);
    expect(refName({ reference: null })).toBe("Production");
    expect(refName(null)).toBe("Production");
    expect(armShort("control", "control")).toBe("Control");
    expect(armShort("control", null)).toBe("Production");
    expect(armLabel("control", "XP-30", "control")).toBe("XP-30 · Control");
    expect(armLabel("control", "XP-2", null)).toBe("Production");
    expect(armColor("control", ["control", "t1"], "control")).toBe(CONTROL_COLOR);
    expect(armColor("control", ["control", "t1"], null)).toBe("var(--accent)");
  });

  it("reads the pair sentence against the reference", () => {
    const s = pairSentence({ arm: "T1", verdict: "win", mean: 0.0039, lo: 0.001, hi: 0.007, sessions: 30, minSessions: 20, mdeAtMin: null, aa: false }, CONTROL_NAME);
    expect(s).toBe("T1 beats Control by 0.39%/day; the whole range is above 0.");
  });
});

describe("Production panel (D108, E25.6)", () => {
  it("splits P − C into start book + residual and counts the open start book", () => {
    expect(productionView(V3)).toEqual({
      pnl: -7289.19,
      ret: -0.0728919,
      dPC: -0.0416,
      startBook: 0.00435,
      residual: -0.04598,
      bookOpen: 1,
      bookTotal: 2,
      rampedFrom: null,
      sessions: 3,
    });
    expect(productionView({ ...V3, production: null } as unknown as ExperimentReport)).toBeNull();
    expect(productionView(null)).toBeNull();
  });

  it("lists Ti − P rows by basis, skipping control and empty bases", () => {
    const rows = productionGapRows(V3);
    expect(rows.map((r) => r.label)).toEqual(["T1 − P · Total", "T1 − P · Ex Start Book"]);
    expect(rows[0]?.est).toBeCloseTo(0.54);
  });

  it("lists the start book with close days", () => {
    expect(startBookRows(V3)).toEqual([
      { id: "sb-1", closed: "Open" },
      { id: "sb-2", closed: "Oct 9" },
    ]);
  });

  it("renders the regime table for one grouping", () => {
    const panel = {
      xps: ["XP-30"],
      sessions: 30,
      thin_n: 10,
      buckets: [
        { grouping: "trend_vol", key: "up/low", n: 4, xps: ["XP-30"], thin: true, d_p_c: { n: 4, mean: -0.0014, ci: { lo: -0.007, hi: 0.0045 } }, ti_minus_c: { all: { n: 12, mean: 0.0002, ci: null } } },
        { grouping: "trend_vol", key: "unknown", n: 26, xps: ["XP-30"], thin: false, d_p_c: { n: 26, mean: null, ci: null }, ti_minus_c: {} },
        { grouping: "trend", key: "up", n: 4, xps: ["XP-30"], thin: true },
      ],
    } as unknown as RegimePanel;
    expect(regimeRows(panel)).toEqual([
      {
        key: "up/low",
        label: "Up · Low",
        n: 4,
        xps: 1,
        thin: true,
        dPC: { value: "−0.14%", ci: "−0.70% to +0.45%" },
        tiC: { value: "+0.02%", ci: null },
      },
      { key: "unknown", label: "Unknown", n: 26, xps: 1, thin: false, dPC: { value: "n/a", ci: null }, tiC: { value: "n/a", ci: null } },
    ]);
    expect(regimeRows(null)).toEqual([]);
  });
});
