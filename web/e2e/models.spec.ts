import { expect, test, type Page } from "@playwright/test";

import { DEBIT_DETAIL, M1_DETAIL, ML2_DETAIL, ML5_DETAIL, ML_MODELS, ML_STUDIES, SHADOW_DETAIL } from "../src/lib/ml.fixture";
import { expectNoOverflow, PHONE_75, PHONE_75_TAG } from "./mobile";

// E27.9 (D133): the Experiments page as Live · Backtest · Model tabs, the model detail page and
// the ML-<n> study page. `/api/ml/*` is fulfilled from src/lib/ml.fixture.ts (the real M1 and
// M2 debit versions read from data/ml on 2026-10-28 plus test_tower_ml.py's synthetic studies),
// because the e2e servers run on scratch stores with no data/ml tree.
const FIXTURE_URL = process.env.ARC_E2E_FIXTURE_URL ?? `http://127.0.0.1:${process.env.ARC_E2E_FIXTURE_PORT ?? "4182"}`;
const VIEWPORTS = [
  { name: "desktop", width: 1440, height: 900 },
  { name: "laptop", width: 1024, height: 900 },
  { name: "w430", width: 430, height: 932 },
  { name: "mobile", width: 390, height: 844 },
] as const;

test.use({ baseURL: FIXTURE_URL });

async function routeMl(page: Page) {
  const studies: Record<string, unknown> = { "ML-2": ML2_DETAIL, "ML-5": ML5_DETAIL };
  const models: Record<string, unknown> = {
    "m1/m1-53833784f276": M1_DETAIL,
    "debit/m2-04e58916e118": DEBIT_DETAIL,
    "credit/m2-aaaaaaaaaaaa": SHADOW_DETAIL,
  };
  await page.route("**/api/ml/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace(/^\/api\/ml\//, "");
    if (path === "models") return route.fulfill({ json: ML_MODELS });
    if (path === "studies") return route.fulfill({ json: ML_STUDIES });
    const m = path.match(/^models\/(.+)$/);
    if (m && models[m[1]!]) return route.fulfill({ json: models[m[1]!] });
    const s = path.match(/^studies\/(ML-\d+)$/);
    if (s && studies[s[1]!]) return route.fulfill({ json: studies[s[1]!] });
    return route.fulfill({ status: 404, json: { error: "not_found", detail: path } });
  });
}

async function open(page: Page, path: string, theme: "light" | "dark" = "dark") {
  await page.addInitScript((t) => localStorage.setItem("arc.theme", t), theme);
  await routeMl(page);
  await page.goto(path);
  await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
}

/** No leaf text inside `scope` is clipped (the truncation probe): scrollWidth within clientWidth. */
async function clippedText(page: Page, scope = "main"): Promise<string[]> {
  return page.evaluate((sel) => {
    const out: string[] = [];
    for (const el of Array.from(document.querySelectorAll<HTMLElement>(`${sel} *`))) {
      if (el.children.length > 0 || el.closest("[data-scroll-x], .arc-scroll-x")) continue;
      const text = el.textContent?.trim() ?? "";
      if (text && el.scrollWidth > el.clientWidth + 1) out.push(`${text} (${el.scrollWidth}>${el.clientWidth})`);
    }
    return out;
  }, scope);
}

test.describe("experiments tabs", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("three tabs, ?tab= drives the body, default Live", async ({ page }) => {
    await open(page, "/experiments");
    const tabs = page.getByTestId("experiments-tabs");
    await expect(tabs.getByRole("tab")).toHaveText(["Live", "Backtest", "Model"]);
    await expect(tabs.getByRole("tab", { name: "Live" })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByRole("heading", { name: "Live Experiments" })).toBeVisible();
    await tabs.getByRole("tab", { name: "Model" }).click();
    await expect(page).toHaveURL(/\?tab=model/);
    await expect(page.getByTestId("model-tab")).toBeVisible();
    await expect(page.getByTestId("experiments-summary")).toContainText("Champions");
    await tabs.getByRole("tab", { name: "Backtest" }).click();
    await expect(page.getByRole("heading", { name: "Backtests" })).toBeVisible();
    await expect(page.getByTestId("experiments-summary")).toContainText("Backtests Done");
  });

  test("old ?show= links land on their tab and are rewritten to ?tab=", async ({ page }) => {
    await open(page, "/experiments?show=backtest");
    await expect(page).toHaveURL(/\?tab=backtest$/);
    await expect(page.getByTestId("experiments-tabs").getByRole("tab", { name: "Backtest" })).toHaveAttribute("aria-selected", "true");
    await open(page, "/experiments?show=all");
    await expect(page).toHaveURL(/\?tab=live$/);
  });

  test("Model tab: groups, rows, studies, How card, footnote", async ({ page }) => {
    await open(page, "/experiments?tab=model");
    await expect(page.getByTestId("model-group").first()).toContainText("Production · 1");
    await expect(page.getByTestId("model-row")).toHaveCount(ML_MODELS.items.length);
    await expect(page.getByTestId("ml-study-row")).toHaveCount(ML_STUDIES.items.length);
    await expect(page.getByTestId("ml-how-steps").locator("li")).toHaveCount(5);
    await expect(page.getByTestId("model-footnote")).toContainText("never P&L");
    // Equal column x across rows: every row's stat grid starts at the same x.
    const xs = await page.getByTestId("row-stats").evaluateAll((els) => els.map((e) => Math.round(e.getBoundingClientRect().x)));
    expect(new Set(xs).size).toBe(1);
  });

  test("model detail: header, strip and the four sections", async ({ page }) => {
    await open(page, "/experiments?tab=model");
    await page.getByTestId("model-row").first().click();
    await expect(page).toHaveURL(/\/experiments\/model\/m1\/m1-53833784f276$/);
    await expect(page.getByTestId("model-detail")).toContainText("m1-53833784f276");
    await expect(page.getByTestId("model-strip")).toContainText("Keep Rule");
    await expect(page.getByTestId("rule-table").getByTestId("rule-row")).toHaveCount(8);
    await expect(page.getByTestId("coverage-chart")).toBeVisible();
    await expect(page.getByTestId("loss-bars")).toBeVisible();
    await expect(page.getByTestId("importance-bars")).toBeVisible();
    const view = page.getByTestId("model-view");
    await view.getByRole("tab", { name: "Spec" }).click();
    await expect(page.getByTestId("spec-card")).toContainText("Features");
    await view.getByRole("tab", { name: "Live" }).click();
    await expect(page.getByTestId("live-pill")).toHaveText("Shadow Off");
    await view.getByRole("tab", { name: "Versions" }).click();
    await expect(page.getByTestId("versions-list")).toContainText("m1-53833784f276");
    await expect(page.getByTestId("cost-card")).toBeVisible();
  });

  test("M2 detail renders the promote rule and the reliability chart", async ({ page }) => {
    await open(page, "/experiments/model/debit/m2-04e58916e118");
    await expect(page.getByTestId("rule-card")).toContainText("Promote Rule");
    await expect(page.getByTestId("reliability-chart")).toBeVisible();
    await expect(page.getByTestId("replay-table")).toBeVisible();
  });

  test("study detail: ladder and the approve hint", async ({ page }) => {
    await open(page, "/experiments?tab=model");
    await page.getByTestId("ml-study-row").filter({ hasText: "ML-5" }).click();
    await expect(page).toHaveURL(/\/experiments\/ML-5$/);
    await expect(page.getByTestId("study-step")).toHaveCount(7);
    await expect(page.getByTestId("study-step").first()).toHaveAttribute("data-state", "done");
    await expect(page.getByTestId("study-hint")).toContainText("approve ML-5");
    await open(page, "/experiments/ML-2");
    await expect(page.getByTestId("ml-study-detail")).toContainText("Passed");
  });
});

for (const vp of VIEWPORTS) {
  test.describe(`model views @ ${vp.name}`, () => {
    test.use({ viewport: { width: vp.width, height: vp.height } });
    for (const theme of ["dark", "light"] as const) {
      test(`no overflow or clipped text (${theme})`, async ({ page }) => {
        await open(page, "/experiments?tab=model", theme);
        await expect(page.getByTestId("model-row").first()).toBeVisible();
        await expectNoOverflow(page);
        expect(await clippedText(page)).toEqual([]);
        await page.screenshot({ path: `e2e/screenshots/model-tab-${vp.name}-${theme}.png`, fullPage: true });
        for (const view of ["results", "spec", "live", "versions"]) {
          await open(page, `/experiments/model/m1/m1-53833784f276?view=${view}`, theme);
          await expect(page.getByTestId("model-strip")).toBeVisible();
          await expectNoOverflow(page);
          expect(await clippedText(page), view).toEqual([]);
          if (view === "results") await page.screenshot({ path: `e2e/screenshots/model-m1-${vp.name}-${theme}.png`, fullPage: true });
        }
        for (const id of ["ML-5", "ML-2"]) {
          await open(page, `/experiments/${id}`, theme);
          await expect(page.getByTestId("study-steps")).toBeVisible();
          await expectNoOverflow(page);
          expect(await clippedText(page), id).toEqual([]);
        }
        await page.screenshot({ path: `e2e/screenshots/model-study-${vp.name}-${theme}.png`, fullPage: true });
      });
    }
  });
}

test.describe(`model views at the owner's phone ${PHONE_75_TAG}`, () => {
  test("Model tab, M1 detail and a study fit at 520", async ({ page }) => {
    for (const [path, id] of [
      ["/experiments?tab=model", "model-tab"],
      ["/experiments/model/m1/m1-53833784f276", "model-page"],
      ["/experiments/ML-5", "ml-study-page"],
    ] as const) {
      await open(page, path);
      await expect(page.getByTestId(id)).toBeVisible();
      await expectNoOverflow(page);
      await page.screenshot({ path: `e2e/screenshots/model-${id}-${PHONE_75.name}.png`, fullPage: true });
    }
  });
});
