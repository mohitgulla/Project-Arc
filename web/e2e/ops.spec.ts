import { expect, test, type Page } from "@playwright/test";

import { PHONE_75, PHONE_75_TAG } from "./mobile";

const OPS_URL = process.env.ARC_E2E_OPS_URL ?? `http://127.0.0.1:${process.env.ARC_E2E_OPS_PORT ?? "4184"}`;

// E8.7d Ops & pipeline on the fixture DB with its ops rows (scripts/tower_fixture_ops.py:
// the dispatcher's dry-run plan for yesterday + today persisted with outcomes and D27
// manifests, anchored at server start) at the TOWER_DESIGN §6 viewports x both themes,
// plus one run detail with the undeclared-write contract mismatch.
const VIEWPORTS = [
  { name: "mobile", width: 390, height: 844 },
  PHONE_75, // E8.8a: the owner's iPhone at 75 % zoom (520x1125); phone-75 project only
  { name: "tablet", width: 768, height: 1024 },
  { name: "desktop", width: 1440, height: 900 },
] as const;
const THEMES = ["dark", "light"] as const;
const CARDS = ["Session Timeline", "Health", "LLM Usage", "Context Store", "Sources", "Order Budget"];

test.use({ baseURL: OPS_URL });

async function open(page: Page, path: string, theme: (typeof THEMES)[number] = "dark") {
  await page.addInitScript((t) => localStorage.setItem("arc.theme", t), theme);
  await page.goto(path);
  await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
}

async function undeclaredRun(page: Page): Promise<string> {
  const res = await page.request.get("/api/ops/runs?job=director&status=ok&size=200&day=today");
  const body = (await res.json()) as { rows: Array<{ run_id: string }> };
  for (const r of body.rows) {
    const d = (await (await page.request.get(`/api/ops/runs/${r.run_id}`)).json()) as {
      step: { contract: { ok: boolean } };
    };
    if (!d.step.contract.ok) return r.run_id;
  }
  throw new Error("fixture has no undeclared-write run");
}

for (const vp of VIEWPORTS) {
  for (const theme of THEMES) {
    test.describe(`ops ${vp.name} ${vp.width}x${vp.height} ${theme}`, { tag: vp.name === PHONE_75.name ? PHONE_75_TAG : [] }, () => {
      test.use({ viewport: { width: vp.width, height: vp.height } });

      test("renders every section of the Ops page", async ({ page }) => {
        await open(page, "/ops", theme);
        await expect(page.getByTestId("ops")).toBeVisible();
        for (const title of CARDS)
          await expect(page.getByRole("heading", { level: 2, name: title, exact: true })).toBeVisible();
        await expect(page.getByTestId("loop-row")).toBeVisible();
        await expect(page.getByTestId("loop-row").locator('[data-status="no_change"]').first()).toBeAttached();
        await expect(page.getByTestId("session-timeline").locator('[data-status="failed"]').first()).toBeAttached();
        await expect(page.getByTestId("health-strip").locator(":scope > li")).toHaveCount(5);
        await expect(page.getByTestId("alerts")).toContainText(/open/);
        await expect(page.getByTestId("halts")).toContainText("daily_loss");
        await expect(page.getByTestId("run-total")).toContainText("runs");
        await expect(page.getByTestId("llm-today")).toContainText("$");
        await expect(page.getByTestId("stacked-bars").locator("svg path").first()).toBeVisible();
        await expect(page.getByTestId("sources")).toContainText("backoff");
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
        expect(overflow).toBeLessThanOrEqual(0);
        await page.screenshot({ path: `e2e/screenshots/ops-${vp.name}-${theme}.png`, fullPage: true });
      });

      test("run detail highlights the undeclared write", async ({ page }) => {
        const runId = await undeclaredRun(page);
        await open(page, `/ops/runs/${runId}`, theme);
        await expect(page.getByTestId("run-detail")).toBeVisible();
        await expect(page.getByTestId("contract-mismatch")).toContainText("write proposal");
        await expect(page.getByTestId("contract").locator("tr[data-mismatch]")).toHaveCount(1);
        await expect(page.getByTestId("chain-steps")).toBeVisible();
        await expect(page.getByTestId("persona-calls")).toContainText("Director");
        await expect(page.getByTestId("run-log")).toContainText("context.undeclared_write");
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
        expect(overflow).toBeLessThanOrEqual(0);
        await page.screenshot({ path: `e2e/screenshots/ops-run-${vp.name}-${theme}.png`, fullPage: true });
      });
    });
  }
}

test.describe("ops behaviour", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("day control and run filters are URL-synced; a run row opens its detail", async ({ page }) => {
    await open(page, "/ops");
    await page.getByRole("button", { name: "Yesterday", exact: true }).click();
    await expect(page).toHaveURL(/day=yesterday/);
    await expect(page.getByTestId("session-timeline").locator('[data-status="future"]')).toHaveCount(0);
    await page.getByLabel("Status").selectOption("failed");
    await expect(page).toHaveURL(/status=failed/);
    await expect(page.getByTestId("run-total")).toHaveText("1 runs");
    await page.getByTestId("runs").getByText("edgar").first().click();
    await expect(page).toHaveURL(/\/ops\/runs\/run-ops-edgar-/);
    await expect(page.getByTestId("run-detail")).toContainText("HTTPError: 503");
  });

  test("a timeline slot opens its run; context links open the entry", async ({ page }) => {
    await open(page, "/ops");
    await page.getByTestId("loop-row").locator('[data-status="done"]').first().click();
    await expect(page).toHaveURL(/\/ops\/runs\/run-ops-director-/);
    await page.getByRole("link", { name: /^candidate:/ }).first().click();
    await expect(page).toHaveURL(/\/ops\/context\//);
    await expect(page.getByTestId("context-entry")).toContainText("candidate");
  });

  test("the effective config section lists keys and the change log", async ({ page }) => {
    await open(page, "/ops");
    await page.getByRole("button", { name: /Effective Config/ }).click();
    await expect(page.getByTestId("config")).toContainText("account_profile");
    await expect(page.getByTestId("config-changes")).toContainText("revert");
  });
});
