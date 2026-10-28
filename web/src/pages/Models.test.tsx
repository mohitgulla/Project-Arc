// D133 (E27.9): models and ML-<n> studies on the Experiments page's Model tab. The model formats
// stored eval numbers only; the list rows, model page and study page render real API bodies.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import {
  costRows,
  coveragePoints,
  featureGroupsText,
  groupModels,
  isMlStudyId,
  liveCells,
  lossGroups,
  modelPath,
  modelStrip,
  outcomeRuleCells,
  parseTab,
  pText,
  ptsText,
  quantileCell,
  relText,
  rulePill,
  statText,
  studyCost,
  studyKindLabel,
  studyNextPill,
  studyRungs,
  targetsOf,
  type MlModelDetailResponse,
  type MlModels,
  type MlStudies,
  type MlStudyDetail,
  type OutcomeDetail,
  type QuantileDetail,
} from "../lib/ml";
import * as F from "../lib/ml.fixture";
import { ModelBody, ModelListRow, ModelTab, StudyBody, StudyListRow } from "./Models";

const ML_MODELS = F.ML_MODELS as unknown as MlModels;
const ML_STUDIES = F.ML_STUDIES as unknown as MlStudies;
const M1_DETAIL = F.M1_DETAIL as unknown as MlModelDetailResponse;
const DEBIT_DETAIL = F.DEBIT_DETAIL as unknown as MlModelDetailResponse;
const SHADOW_DETAIL = F.SHADOW_DETAIL as unknown as MlModelDetailResponse;
const ML2_DETAIL = F.ML2_DETAIL as unknown as MlStudyDetail;
const ML5_DETAIL = F.ML5_DETAIL as unknown as MlStudyDetail;

function render(node: React.ReactNode, at = "/experiments?tab=model"): string {
  return renderToStaticMarkup(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter initialEntries={[at]}>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

const plain = (html: string) =>
  html
    .replace(/<[^>]+>/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/\s+/g, " ");

const M1 = M1_DETAIL.detail as QuantileDetail;
const DEBIT = DEBIT_DETAIL.detail as OutcomeDetail;

describe("ml model", () => {
  it("tabs: ?tab= wins, old ?show= links still land, default Live", () => {
    expect(parseTab("model")).toBe("model");
    expect(parseTab(null, "backtest")).toBe("backtest");
    expect(parseTab("model", "live")).toBe("model");
    expect(parseTab(null, "all")).toBe("live");
    expect(parseTab("x")).toBe("live");
  });

  it("ids and paths", () => {
    expect(isMlStudyId("ML-5")).toBe(true);
    expect(isMlStudyId("ML-0")).toBe(false);
    expect(isMlStudyId("BT-5")).toBe(false);
    expect(modelPath("m1", "m1-53833784f276")).toBe("/experiments/model/m1/m1-53833784f276");
  });

  it("number text: true minus, coverage fractions as points", () => {
    expect(relText(-0.329531)).toBe("−33%");
    expect(relText(0.160835, 1)).toBe("+16.1%");
    expect(ptsText(0.409)).toBe("40.9 pts");
    expect(ptsText(null)).toBe("—");
    expect(pText(9.7e-18)).toBe("9.7e-18");
    expect(pText(0.9997)).toBe("1");
    expect(pText(0.031)).toBe("0.03");
    expect(statText({ value: 4, of: 8, unit: "count" })).toBe("4 / 8");
    expect(statText({ value: -5264.57, unit: "usd" })).toBe("−$5.3K");
    expect(statText({ value: 0.146, unit: "ratio" })).toBe("0.15");
    expect(statText({ value: null, unit: "pct" })).toBe("—");
  });

  it("registry list groups by status, real M1 shipped baselines", () => {
    const g = groupModels(ML_MODELS.items);
    expect(g.map((x) => x.title)).toEqual(["Production", "Candidate"]);
    const m1 = ML_MODELS.items.find((r) => r.family === "m1")!;
    expect(rulePill(m1)).toEqual({ label: "Baselines Shipped", tone: "neutral" });
    const debit = ML_MODELS.items.find((r) => r.family === "debit")!;
    expect(rulePill(debit)).toEqual({ label: "Rule Not Met", tone: "neutral" });
    // Holdout reads are a count, never a ratio.
    expect(statText(M1.headline.find((s) => s.label === "Holdout Reads")!)).toBe("0");
  });

  it("Model tab strip reads the summary, not the Live/Backtest counts", () => {
    const s = modelStrip(ML_MODELS, ML_STUDIES);
    expect(s.map((x) => x.label)).toEqual(["Champions", "In Shadow", "ML Studies", "Live Calibration", "Feature Drift", "Tonight"]);
    expect(s[0]).toMatchObject({ value: "1 / 3", sub: "M1 · baselines" });
    expect(s[2]).toMatchObject({ value: "1 · 1", sub: "proposed · queued" });
    expect(s[3]!.value).toBe("—");
    expect(s[5]).toMatchObject({ value: "Idle", sub: "shares 21:30 ET slot" });
  });

  it("M1 detail: rule cells, coverage, loss groups, features", () => {
    expect(targetsOf(M1)).toContain("Vol h21");
    const vol = M1.rule.find((r) => r.target === "Vol h21")!;
    const c = quantileCell(vol.years.find((y) => y.year === "2023")!);
    expect(c.loss).toMatch(/^−\d+\.\d%$/);
    expect(c.cov).toMatch(/ pts$/);
    const pts = coveragePoints(M1, "Vol h21", "2023");
    expect(pts.length).toBeGreaterThan(2);
    expect(pts.every((p) => p.nominal === p.perfect)).toBe(true);
    expect(pts.some((p) => p.lgbm != null && p.baseline != null)).toBe(true);
    expect(lossGroups(M1).map((g) => g.group)).toEqual(expect.arrayContaining(["Vol", "Return"]));
    expect(featureGroupsText({ ticker_price: 57, ticker_options: 9, market: 12 })).toBe("Ticker Price 57 · Market 12 · Ticker Options 9");
  });

  it("M2 detail: promote rule cells read the stored replay", () => {
    const r2023 = DEBIT.rule.find((r) => r.year === "2023")!;
    const cells = outcomeRuleCells(r2023);
    expect(cells.map((c) => c.label)).toEqual(["vs Logistic", "vs Managed PoP", "E[R] IC", "Replay Net", "Replay Max DD"]);
    expect(cells[0]).toMatchObject({ value: "+6.6%", ok: false });
    expect(cells[2]).toMatchObject({ value: "0.15 · t 14.5", ok: true });
    expect(cells[3]!.value).toBe("−$6.3K vs −$1.1K");
  });

  it("live cells stay dashes until a health record exists", () => {
    expect(liveCells(null).every((c) => c.value === "—")).toBe(true);
    const l = liveCells(SHADOW_DETAIL.detail.live);
    expect(l[0]).toMatchObject({ value: "Insufficient", sub: "n 4, needs 30" });
    expect(l[1]).toMatchObject({ value: "0.31", sub: "iv_rank · 1 over 0.25" });
    expect(l[4]!.value).toBe("95%");
    expect(l[5]!.value).toBe("2026-10-01");
  });

  it("cost rows only list stored timings", () => {
    expect(costRows(null)).toEqual([]);
    const rows = costRows(M1.cost);
    expect(rows.length).toBeGreaterThan(0);
    expect(rows.every((r) => r.value !== "—")).toBe(true);
  });

  it("study pills, rungs and cost", () => {
    const [ml4, ml5, ml2] = ["ML-4", "ML-5", "ML-2"].map((id) => ML_STUDIES.items.find((s) => s.study_id === id)!);
    expect(studyNextPill(ml5!)).toEqual({ label: "Awaiting Approve", tone: "neutral" });
    expect(studyNextPill(ml4!)).toEqual({ label: "Queue #2", tone: "neutral" });
    expect(studyNextPill(ml2!)).toEqual({ label: "Passed", tone: "pos" });
    expect(studyKindLabel(ml4!)).toBe("Features · M1");
    expect(studyRungs(ml4!).map((r) => r.label)).toEqual(["Approved", "Dev 2022", "Dev 2023", "Verdict", "Shadow", "Champion"]);
    expect(studyRungs(ml2!).filter((r) => r.done).length).toBeGreaterThanOrEqual(5);
    expect(studyCost(ml4!)).toBe("3.3 h left");
  });
});

describe("ml views", () => {
  it("Model tab renders the registry, studies and the How card", () => {
    const t = plain(render(<ModelTab models={ML_MODELS} studies={ML_STUDIES} loading={false} error={null} />));
    expect(t).toContain("Production · 1");
    expect(t).toContain("M1 · Move & Vol");
    expect(t).toContain("m1-53833784f276");
    expect(t).toContain("Baselines Shipped");
    expect(t).toContain("ML-5");
    expect(t).toContain("Awaiting Approve");
    expect(t).toContain("How ML Studies Run");
    expect(t).toContain("never P&L");
    expect(t).not.toMatch(/\$\d/);
  });

  it("empty Model tab says what feeds it", () => {
    const t = plain(render(<ModelTab models={undefined} studies={undefined} loading={false} error={null} />));
    expect(t).toContain("No models yet.");
    expect(t).toContain("nothing runs without approve ML-<n>");
  });

  it("list rows link to the model and study pages", () => {
    const html = render(<ModelListRow r={ML_MODELS.items[0]!} />);
    expect(html).toContain('href="/experiments/model/m1/m1-53833784f276"');
    // Study rows use the shared ListRowFrame (a button that navigates), like XP/BT rows.
    const s = plain(render(<StudyListRow s={ML_STUDIES.items[0]!} />));
    expect(s).toContain("ML-4");
    expect(s).toContain("Queue #2");
    expect(s).toContain("Incumbent m1-53833784f276");
  });

  it("M1 page: Results renders the keep-rule table and charts", () => {
    const t = plain(render(<ModelBody d={M1} view="results" />));
    expect(t).toContain("Keep Rule");
    expect(t).toContain("Vol h21");
    expect(t).toContain("Coverage");
    expect(t).toContain("Pinball vs Baseline");
    expect(t).toContain("Feature Importance");
    expect(t).not.toContain("NaN");
  });

  it("M1 page: Spec, Live and Versions views", () => {
    expect(plain(render(<ModelBody d={M1} view="spec" />))).toContain("Features");
    const live = plain(render(<ModelBody d={M1} view="live" />));
    expect(live).toContain("Live Monitoring");
    expect(live).toContain("Shadow Off");
    const v = plain(render(<ModelBody d={M1} view="versions" />));
    expect(v).toContain("m1-53833784f276");
    expect(v).toContain("Cost");
  });

  it("M2 page renders the promote rule and the replay", () => {
    const t = plain(render(<ModelBody d={DEBIT} view="results" />));
    expect(t).toContain("Promote Rule");
    expect(t).toContain("Reliability");
    expect(t).toContain("Replay");
    expect(t).toContain("Not Met");
  });

  it("study pages: proposed shows the approve hint, a pass names the shadow version", () => {
    const p = plain(render(<StudyBody d={ML5_DETAIL} />));
    expect(p).toContain("ML-5");
    expect(p).toContain("approve ML-5");
    expect(p).toContain("rungs run after approve");
    const d = plain(render(<StudyBody d={ML2_DETAIL} />));
    expect(d).toContain("Passed");
    expect(d).toMatch(/is in shadow/);
  });
});
