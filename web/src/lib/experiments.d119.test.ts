// E25.8 (D120): the SPY-adjusted primary as the headline, the unadjusted alongside, the
// β̂ / R² / HAC / σ_LR chips, the verdict-basis note and the "mostly market beta" flag.
import { describe, expect, it } from "vitest";

import { K3_D119_DETAIL, K3_D119_ROW } from "./experiments.d119.fixture";
import {
  ADJUSTED_LABEL,
  adjustedPairs,
  controlPairs,
  estimateText,
  headlineEstimate,
  listPrimary,
  mostlyBeta,
  otherLine,
  resultRows,
  statChips,
  verdictBasis,
  verdictNote,
  type Adjustment,
  type ExperimentReport,
  type PrimaryLike,
} from "./experiments";

const CI = (lo: number, hi: number) => ({ lo, hi, level: 0.95, estimate: (lo + hi) / 2 });

/** A fitted descriptive adjustment (spec without `analysis`, e.g. XP-1 once n >= 3). */
function adj(over: Partial<Adjustment> = {}): Adjustment {
  return {
    pre_registered: false,
    status: "fitted",
    covariates: ["spy_ret"],
    n: 12,
    covariate_missing: [],
    beta: 0.3,
    beta_se: 0.12,
    r2: 0.41,
    estimate: 0.0004,
    ci: CI(-0.002, 0.0028),
    p_value: 0.61,
    tau: 0.004,
    sigma_source: "running_corrected",
    ...over,
  } as Adjustment;
}

/** D44 numbers at the top level, descriptive D119 fields alongside (no `primary_unadjusted`). */
function descriptive(a: Adjustment | null, extra: Partial<PrimaryLike> = {}): PrimaryLike {
  return {
    mean: 0.0012,
    ci: CI(-0.003, 0.0054),
    p_value: 0.72,
    adjustment: a,
    hac: { lag: 5, factor: 1.8, factor_running: 1.8, rho: [0.2, 0.1, 0, 0, 0], source: "running" },
    sigma_lr: 0.0052,
    ...extra,
  };
}

describe("headlineEstimate: every branch (D120)", () => {
  it("fitted: the SPY-adjusted intercept is the headline, the unadjusted D44 numbers second", () => {
    const h = headlineEstimate(descriptive(adj()));
    expect(h.basis).toBe("adjusted");
    expect(h.label).toBe(ADJUSTED_LABEL);
    expect([h.est, h.lo, h.hi, h.p]).toEqual([0.0004, -0.002, 0.0028, 0.61]);
    expect(h.chip).toBeUndefined();
    expect(otherLine(h)).toBe("Unadjusted: +0.12% [−0.30, +0.54] · p 0.72");
  });

  it("pre-registered: the adjusted numbers are top-level, the second line reads primary_unadjusted", () => {
    const a = adj({ pre_registered: true });
    const p: PrimaryLike = {
      mean: 0.0004,
      ci: a.ci,
      p_value: 0.61,
      adjustment: a,
      primary_unadjusted: { n: 12, mean: 0.0012, ci: CI(-0.003, 0.0054), p_value: 0.72, sigma: 0.004, sigma_source: "running_corrected", tau: 0.004 },
    };
    const h = headlineEstimate(p);
    expect(h.basis).toBe("adjusted");
    expect(estimateText(h)).toBe("+0.04% [−0.20, +0.28] · p 0.61");
    expect(otherLine(h)).toBe("Unadjusted: +0.12% [−0.30, +0.54] · p 0.72");
  });

  it("insufficient_n (n < 3): unadjusted headline with the pending chip", () => {
    const a = adj({ status: "insufficient_n", n: 2, beta: null, beta_se: null, r2: null, estimate: null, ci: null, p_value: null });
    const h = headlineEstimate(descriptive(a));
    expect(h.basis).toBe("unadjusted");
    expect(h.reason).toBe("insufficient_n");
    expect(h.chip).toEqual({ label: "SPY adjustment pending (n < 3)", tone: "neutral" });
    expect(h.est).toBe(0.0012);
    expect(h.other).toBeNull();
  });

  it("insufficient_n with n >= 3 is no SPY variance (Sxx = 0)", () => {
    const h = headlineEstimate(descriptive(adj({ status: "insufficient_n", n: 6, estimate: 0.0012 })));
    expect(h.reason).toBe("no_variance");
    expect(h.chip?.label).toBe("no SPY variance");
  });

  it("covariate_missing on a pre-registered spec: SPY day missing, verdict on hold (warn)", () => {
    const a = adj({ pre_registered: true, covariate_missing: ["2026-10-16"] });
    const p: PrimaryLike = {
      mean: 0.0004,
      ci: a.ci,
      p_value: 0.61,
      adjustment: a,
      primary_unadjusted: { n: 13, mean: 0.0011, ci: CI(-0.003, 0.0052), p_value: 0.7, sigma: 0.004, sigma_source: "running_corrected", tau: 0.004 },
    };
    const h = headlineEstimate(p);
    expect(h.basis).toBe("unadjusted");
    expect(h.reason).toBe("covariate_missing");
    expect(h.chip).toEqual({ label: "SPY day missing — verdict on hold", tone: "warn" });
    expect(h.est).toBe(0.0011); // the D44 number, never the partial fit
    expect(otherLine(h)).toBe("SPY-adjusted (partial): +0.04% [−0.20, +0.28] · p 0.61");
  });

  it("covariate_missing on a descriptive spec: no 'verdict on hold' (its verdict never waits)", () => {
    const h = headlineEstimate(descriptive(adj({ covariate_missing: ["2026-10-16"] })));
    expect(h.chip?.label).toBe("SPY day missing");
  });

  it("pre-D119 report (no adjustment): the plain unadjusted headline, no chip, no second line", () => {
    const h = headlineEstimate({ mean: 0.0012, ci: CI(-0.003, 0.0054), p_value: 0.72 });
    expect(h).toEqual({ basis: "unadjusted", label: "Unadjusted", est: 0.0012, lo: -0.003, hi: 0.0054, p: 0.72, other: null });
    expect(otherLine(h)).toBeNull();
    expect(statChips({ mean: 0.0012 })).toEqual([]);
  });

  it("a spec pre-registering covariates: [] (status unadjusted) falls back with its own chip", () => {
    const h = headlineEstimate(descriptive(adj({ status: "unadjusted", covariates: [], beta: null })));
    expect(h.basis).toBe("unadjusted");
    expect(h.reason).toBe("unadjusted_spec");
  });
});

describe("verdict basis (D120: verdicts are unchanged)", () => {
  it("a spec without analysis (XP-1) decides on unadjusted; an adjusted headline says so", () => {
    const vb = verdictBasis({ analysis: null });
    expect(vb).toEqual({ basis: "unadjusted", preAdjustment: true });
    expect(verdictNote(headlineEstimate(descriptive(adj())), vb)).toBe("verdict on unadjusted — spec pre-dates the SPY adjustment");
  });

  it("a pre-registered [spy_ret] spec: headline and verdict match, no note", () => {
    const vb = verdictBasis({ analysis: { covariates: ["spy_ret"], hac_lag: 5 } });
    expect(vb).toEqual({ basis: "adjusted", preAdjustment: false });
    expect(verdictNote(headlineEstimate(descriptive(adj({ pre_registered: true }))), vb)).toBeNull();
  });

  it("no note while the headline is unadjusted (n < 3 on XP-1)", () => {
    const a = adj({ status: "insufficient_n", n: 2, estimate: null, beta: null });
    expect(verdictNote(headlineEstimate(descriptive(a)), verdictBasis(null))).toBeNull();
  });
});

describe("owner-amended analysis (D122)", () => {
  const AMENDED = { at: "2026-10-16T20:00:00-04:00", sessions_at_amend: 3, from: null, to: { covariates: ["spy_ret" as const], hac_lag: 5 } };

  it("the verdict chip says SPY-adjusted (amended at session n), not pre-registered", () => {
    const vb = verdictBasis({ analysis: { covariates: ["spy_ret"], hac_lag: 5 } }, AMENDED);
    expect(vb).toEqual({ basis: "adjusted", preAdjustment: false, amendedAt: 3 });
    const note = verdictNote(headlineEstimate(descriptive(adj({ pre_registered: true }))), vb);
    expect(note).toBe("verdict on SPY-adjusted (amended at session 3)");
    expect(note).not.toMatch(/pre-registered|D1\d\d/);
  });

  it("the chip shows even while the headline falls back (n < 3)", () => {
    const a = adj({ pre_registered: true, status: "insufficient_n", n: 2, estimate: null, beta: null });
    const vb = verdictBasis({ analysis: { covariates: ["spy_ret"], hac_lag: 5 } }, AMENDED);
    expect(verdictNote(headlineEstimate(descriptive(a)), vb)).toBe("verdict on SPY-adjusted (amended at session 3)");
  });

  it("amended back to none reads unadjusted (amended), never 'pre-dates D119'", () => {
    const vb = verdictBasis({ analysis: null }, { ...AMENDED, sessions_at_amend: 5 });
    expect(vb.preAdjustment).toBe(false);
    expect(verdictNote(headlineEstimate(descriptive(adj())), vb)).toBe("verdict on unadjusted (amended at session 5)");
  });

  it("adjustedPairs carries the amended note for every pair", () => {
    const r = { ...(K3_D119_DETAIL.report as ExperimentReport), analysis_amended: AMENDED };
    const blocks = adjustedPairs(r, K3_D119_DETAIL.spec);
    expect(blocks.length).toBeGreaterThan(0);
    expect(blocks.every((b) => b.verdictNote === "verdict on SPY-adjusted (amended at session 3)")).toBe(true);
    // the unamended fixture is unchanged: no note
    expect(adjustedPairs(K3_D119_DETAIL.report as ExperimentReport, K3_D119_DETAIL.spec).every((b) => b.verdictNote === null)).toBe(true);
  });
});

describe("stat chips and the mostly-market-beta flag", () => {
  it("β̂ ± SE, R², HAC f (source), σ_LR, each with a plain-words tip", () => {
    const chips = statChips(descriptive(adj({ beta: -0.3 })));
    expect(chips.map((c) => c.label)).toEqual(["β̂ −0.30 ± 0.12", "R² 0.41", "HAC f 1.8 (running)", "σ_LR 0.52%"]);
    expect(chips.every((c) => c.tip.length > 20)).toBe(true);
    const cap = statChips(descriptive(adj(), { hac: { lag: 5, factor: 4, source: "cap", rho: [] } }));
    expect(cap.find((c) => c.key === "hac")?.label).toBe("HAC f 4.0 (cap)");
  });

  it("flags when |adj − unadj| > σ_LR/√n and |β̂| > 2·SE", () => {
    // gap 0.0008 vs SE 0.0052/√12 = 0.0015: not over one SE
    expect(mostlyBeta(descriptive(adj()))).toBe(false);
    // gap 0.0040 > 0.0015 and |β̂| 0.3 > 0.24
    expect(mostlyBeta(descriptive(adj(), { mean: 0.0044 }))).toBe(true);
    // big gap but β̂ inside 2·SE: not beta
    expect(mostlyBeta(descriptive(adj({ beta_se: 0.2 }), { mean: 0.0044 }))).toBe(false);
    // not fitted / pre-D119: never
    expect(mostlyBeta(descriptive(adj({ status: "insufficient_n" }), { mean: 0.0044 }))).toBe(false);
    expect(mostlyBeta({ mean: 0.0044 })).toBe(false);
  });
});

describe("the D119 K=3 fixture (real tower API output)", () => {
  const r = K3_D119_DETAIL.report as ExperimentReport;

  it("every Ti − C pair headlines its SPY-adjusted intercept", () => {
    const pairs = controlPairs(r);
    expect(pairs.map((p) => p.adjustment?.status)).toEqual(["fitted", "fitted", "fitted"]);
    const rows = resultRows(r);
    rows.forEach((x, i) => expect(x.est / 100).toBeCloseTo(pairs[i]?.adjustment?.estimate ?? NaN, 12));
    const blocks = adjustedPairs(r, K3_D119_DETAIL.spec);
    expect(blocks.map((b) => b.headline.basis)).toEqual(["adjusted", "adjusted", "adjusted"]);
    expect(blocks.every((b) => b.verdictNote === null)).toBe(true);
    expect(blocks.map((b) => b.verdict.label)).toEqual(["Win", "No Call Yet", "No Call Yet"]);
  });

  it("the D120 gap rule on the fixture: T1 trips it, T2 sits just under one SE, T3 has no beta", () => {
    // T1: adj +0.396% vs unadj +0.474% (gap 0.078%), SE = σ_LR/√24 = 0.029%, β̂ 0.58 ± 0.03.
    // T2: adj +0.022% vs unadj +0.043% (gap 0.0211%), SE 0.0212%: one hair under the bar.
    const blocks = adjustedPairs(r, K3_D119_DETAIL.spec);
    expect(Object.fromEntries(blocks.map((b) => [b.arm, b.mostlyBeta]))).toEqual({ T1: true, T2: false, T3: false });
    expect(blocks[1]?.chips[0]?.label).toMatch(/^β̂ 0\.57 ± 0\.02$/);
  });

  it("the list row headlines the adjusted effect too", () => {
    const h = headlineEstimate(listPrimary(K3_D119_ROW));
    expect(h.basis).toBe("adjusted");
    expect(h.est).toBe(K3_D119_ROW.adjustment?.estimate);
  });

  it("a pre-D119 report shows no adjustment panel", () => {
    const old = { ...r, primary: { ...r.primary, adjustment: undefined }, pairwise: (r.pairwise ?? []).map((p) => ({ ...p, adjustment: undefined, primary_unadjusted: undefined })) } as ExperimentReport;
    expect(adjustedPairs(old, null)).toEqual([]);
  });
});
