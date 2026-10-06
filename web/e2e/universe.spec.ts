import { expect, test, type Page } from "@playwright/test";

import { expectMinFontSize, expectNoOverflow, expectTouchTargets, PHONE_75, PHONE_75_TAG, viewportUse } from "./mobile";

const OPS_URL = process.env.ARC_E2E_OPS_URL ?? `http://127.0.0.1:${process.env.ARC_E2E_OPS_PORT ?? "4184"}`;

// E12.6 (D51): the Universe page on the --ops fixture (scripts/tower_fixture_ops.py
// add_universe: core 20 + momentum 15 + trending 6 + discovery 9 active, 2 over the cap,
// a partial momentum feed and the 100-name pre-D51 `universe` override, which is ignored).
const VIEWPORTS = [
  PHONE_75, // the owner's iPhone at 75 % zoom (520 CSS px)
  { name: "desktop", width: 1440, height: 900 },
] as const;
const THEMES = ["dark", "light"] as const;
const TIERS = ["Core", "Momentum", "Trending", "Discovery"];

test.use({ baseURL: OPS_URL });

async function open(page: Page, path: string, theme: (typeof THEMES)[number] = "dark") {
  await page.addInitScript((t) => localStorage.setItem("arc.theme", t), theme);
  await page.goto(path);
  await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
}

for (const vp of VIEWPORTS) {
  for (const theme of THEMES) {
    test.describe(`universe ${vp.name} ${vp.width}x${vp.height} ${theme}`, { tag: vp.name === PHONE_75.name ? PHONE_75_TAG : [] }, () => {
      test.use(viewportUse(vp));

      test("renders the summary, tiers in order, dropped and market reference", async ({ page }) => {
        await open(page, "/ops/universe", theme);
        const root = page.getByTestId("ops-universe");
        await expect(root.getByRole("heading", { level: 1, name: "Universe" })).toBeVisible();
        await expect(root.getByTestId("uni-summary-line")).toHaveText("Active 50/50 · Core 20 · Momentum 15 · Trending 6 · Discovery 9");
        await expect(root.getByTestId("uni-state")).toHaveText("Resolved Today");
        await expect(root.getByTestId("uni-age")).toContainText(/as of \d+m ago/);
        // tier sections in precedence order, Title Case
        const titles = await root.getByTestId("uni-tier").locator("h2").allTextContents();
        expect(titles).toEqual(TIERS);
        const core = root.locator('[data-testid=uni-tier][data-tier="core"]');
        await expect(core.getByTestId("uni-chip")).toHaveCount(20);
        const mom = root.locator('[data-testid=uni-tier][data-tier="momentum"]');
        await expect(mom.getByTestId("uni-partial")).toHaveText("Partial");
        await expect(mom.getByTestId("uni-tier-counts")).toHaveText("offered 24 · active 15 / 25");
        await expect(mom.getByTestId("uni-tier-meta")).toContainText("source stockanalysis");
        await expect(mom.getByTestId("uni-tier-meta")).toContainText("refreshed 3d ago");
        await expect(root.getByTestId("uni-expired")).toHaveCount(0);
        // dropped list: tier + reason
        const drops = root.getByTestId("uni-drop");
        await expect(drops).toHaveCount(2);
        await expect(drops.first()).toContainText("SNDK");
        await expect(drops.first()).toContainText("Discovery");
        await expect(drops.first()).toContainText("past the active-list cap");
        await expect(root.getByTestId("uni-market-ref-line")).toHaveText("SPY QQQ (regime only, not traded)");
        await expect(root.getByTestId("uni-override-ignored")).toContainText("100 names");
        await expectNoOverflow(page);
        if (vp.width <= 520) {
          await expectTouchTargets(page, "[data-testid=uni-chips]");
          await expectMinFontSize(page, "[data-testid=ops-universe]");
        }
        await page.screenshot({ path: `e2e/screenshots/universe-${vp.name}-${theme}.png`, fullPage: true });
      });

      test("a chip shows source, reason and also-in", async ({ page }) => {
        await open(page, "/ops/universe", theme);
        const aapl = page.locator('[data-testid=uni-chip][data-ticker="AAPL"]');
        await aapl.getByRole("button").click();
        const tip = page.getByRole("tooltip");
        await expect(tip).toContainText("in Core · source config");
        await expect(tip).toContainText("also in Momentum");
        await page.keyboard.press("Escape");
        await page.locator('[data-testid=uni-chip][data-ticker="RKLB"]').getByRole("button").click();
        await expect(page.getByRole("tooltip")).toContainText("reddit #1");
      });
    });
  }
}

test("config page labels the core universe, warns it is ignored, links to Universe", async ({ page }) => {
  await open(page, "/ops/config");
  const uni = page.locator('[data-testid=cfg-key][data-key="universe"]');
  await expect(uni.getByTestId("cfg-key-label")).toHaveText("Core Universe (≤ 30)");
  await expect(uni.getByTestId("cfg-core-ignored")).toContainText("100 names > 30");
  await uni.getByTestId("cfg-universe-link").click();
  await expect(page).toHaveURL(/\/ops\/universe$/);
  await expect(page.getByTestId("uni-summary")).toBeVisible();
});

test("ops page links to the Universe page", async ({ page }) => {
  await open(page, "/ops");
  await expect(page.getByTestId("universe-open")).toHaveAttribute("href", "/ops/universe");
});

test("the universe API is GET only", async ({ page }) => {
  expect((await page.request.get("/api/ops/universe")).status()).toBe(200);
  expect((await page.request.post("/api/ops/universe")).status()).toBe(405);
});
