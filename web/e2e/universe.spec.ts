import { expect, test, type Page } from "@playwright/test";

import { expectMinFontSize, expectNoOverflow, expectTouchTargets, PHONE_75, PHONE_75_TAG, viewportUse } from "./mobile";

const OPS_URL = process.env.ARC_E2E_OPS_URL ?? `http://127.0.0.1:${process.env.ARC_E2E_OPS_PORT ?? "4184"}`;

// E12.6 (D56 since E13.15): the Universe page on the --ops fixture
// (scripts/tower_fixture_ops.py add_universe: core 20 + momentum 11 + discovery 19 active,
// 4 momentum names past the tier size and 1 discovery name past the cap, a partial momentum
// feed and the 100-name pre-D51 `universe` override, which is ignored). E14.8 (D64): one table
// per tier; discovery carries two-run scores (NBIS carried) and three Stocktwits readings
// (QCOM 80 % bull, CRWV 30 %, NBIS too few tags).
const VIEWPORTS = [
  PHONE_75, // the owner's iPhone at 75 % zoom (520 CSS px)
  { name: "desktop", width: 1440, height: 900 },
] as const;
const THEMES = ["dark", "light"] as const;
const TIERS = ["Core", "Momentum", "Discovery", "Trending"];

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
        await expect(root.getByTestId("uni-summary-line")).toHaveText("Active 50/50 · Core 20 · Momentum 11 · Discovery 19 · Trending 0");
        await expect(root.getByTestId("uni-state")).toHaveText("Resolved Today");
        await expect(root.getByTestId("uni-age")).toContainText(/as of \d+m ago/);
        // tier sections in precedence order, Title Case
        const titles = await root.getByTestId("uni-tier").locator("h2").allTextContents();
        expect(titles).toEqual(TIERS);
        const core = root.locator('[data-testid=uni-tier][data-tier="core"]');
        await expect(core.getByTestId("uni-row")).toHaveCount(20);
        await expect(core.getByTestId("uni-tier-counts")).toHaveText("20 names");
        const mom = root.locator('[data-testid=uni-tier][data-tier="momentum"]');
        await expect(mom.getByTestId("uni-partial")).toHaveText("Partial");
        // E14.8: tier size vs feed rows, never "offered"
        await expect(mom.getByTestId("uni-tier-counts")).toHaveText("top 20 of 24 listed");
        await expect(mom.getByTestId("uni-tier-active")).toHaveText("active 11 / 20");
        await expect(root).not.toContainText("offered");
        // momentum SPMO weight column (parsed from the pre-v5 reason)
        await expect(mom.getByTestId("uni-row").first().locator("[data-col=weight]")).toHaveText(/^\d+\.\d{2}%$/);
        const disc = root.locator('[data-testid=uni-tier][data-tier="discovery"]');
        await expect(disc.getByTestId("uni-tier-counts")).toHaveText("20 names (1 carried)");
        const qcom = disc.locator('[data-testid=uni-row][data-ticker="QCOM"]');
        await expect(qcom.locator("[data-col=score]")).toHaveText("0.86");
        await expect(qcom.getByTestId("uni-st")).toHaveText("80% bull");
        await expect(qcom.getByTestId("uni-st")).toHaveClass(/text-pos-text/);
        await expect(disc.locator('[data-testid=uni-row][data-ticker="CRWV"]').getByTestId("uni-st")).toHaveClass(/text-neg-text/);
        const nbis = disc.locator('[data-testid=uni-row][data-ticker="NBIS"]');
        await expect(nbis.getByTestId("uni-carried")).toHaveText("carried");
        await expect(nbis.getByTestId("uni-st")).toHaveText("—");
        if (vp.width > 768) {
          await expect(qcom.locator("[data-col=today-prev]")).toHaveText("0.86 / 0.70");
          await expect(nbis.locator("[data-col=today-prev]")).toHaveText("— / 0.70");
          await expect(qcom.locator("[data-col=sources]")).toHaveText("Arete Trading, FX Evolution");
          await expect(disc.locator('[data-testid=uni-row][data-ticker="CRWV"] [data-col=stance]')).toHaveText("Bearish");
          await expect(qcom.locator("[data-col=picked]")).toBeVisible();
        } else {
          // phones: ticker + 2 key columns, the rest in the row's tap detail
          await expect(qcom.locator("[data-col=sources]")).toBeHidden();
          await expect(qcom.locator("[data-col=picked]")).toBeHidden();
        }
        await expect(mom.getByTestId("uni-tier-meta")).toContainText("source stockanalysis");
        await expect(mom.getByTestId("uni-tier-meta")).toContainText("refreshed 3d ago");
        await expect(root.getByTestId("uni-expired")).toHaveCount(0);
        // dropped list: tier + reason
        const drops = root.getByTestId("uni-drop");
        await expect(drops).toHaveCount(5);
        await expect(drops.first()).toContainText("KKR");
        await expect(drops.first()).toContainText("Momentum");
        await expect(drops.first()).toContainText("past the tier's size");
        await expect(drops.last()).toContainText("BBAI");
        await expect(drops.last()).toContainText("past the active-list cap");
        await expect(root.getByTestId("uni-market-ref-line")).toHaveText("SPY QQQ IWM (regime only, not traded)");
        await expect(root.getByTestId("uni-override-ignored")).toContainText("100 names");
        await expectNoOverflow(page);
        if (vp.width <= 520) {
          await expectTouchTargets(page, "[data-testid=uni-table] tbody");
          await expectMinFontSize(page, "[data-testid=ops-universe]");
        }
        await page.screenshot({ path: `e2e/screenshots/universe-${vp.name}-${theme}.png`, fullPage: true });
      });

      test("a row expands to in-tier, Stocktwits, reason and also-in", async ({ page }) => {
        await open(page, "/ops/universe", theme);
        const aapl = page.locator('[data-testid=uni-row][data-ticker="AAPL"]');
        await expect(aapl.getByTestId("uni-also")).toHaveText("also in Momentum");
        await aapl.getByTestId("uni-row-toggle").click();
        const d = page.locator('[data-testid=uni-row-detail][data-ticker="AAPL"]');
        await expect(d).toContainText("core list");
        await expect(d).toContainText("also in Momentum");
        await page.locator('[data-testid=uni-row][data-ticker="QCOM"]').getByTestId("uni-row-toggle").click();
        const q = page.locator('[data-testid=uni-row-detail][data-ticker="QCOM"]');
        await expect(q).toContainText(/In tier \d+ of the last 20 sessions/);
        await expect(q).toContainText("Stocktwits: 80% bull (10 tagged");
        await expect(q).toContainText("YouTube call");
        if (vp.width <= 768) await expect(q).toContainText("Arete Trading, FX Evolution");
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

// E14.8: Today's Pick (Overview) on the same store, at 360 / 768 / 1280: score · ST per row;
// at 360 the sentiment stacks under the score in micro size.
for (const { name, width } of [
  { name: "w360", width: 360 },
  { name: "w768", width: 768 },
  { name: "w1280", width: 1280 },
]) {
  for (const theme of THEMES) {
    test.describe(`today's pick ${name} ${theme}`, () => {
      test.use({ viewport: { width, height: 900 } });
      test("Today's Pick shows score · Stocktwits sentiment", async ({ page }) => {
        await open(page, "/", theme);
        const picks = page.getByTestId("picks");
        await expect(picks.getByTestId("pick-legend").first()).toBeVisible();
        const q = picks.locator('[data-testid=pick-row][data-ticker="QCOM"]');
        await expect(q.getByTestId("pick-score")).toHaveText("0.86");
        await expect(q.getByTestId("pick-st")).toHaveText("80% bull");
        await expect(q.getByTestId("pick-st")).toHaveClass(/text-pos-text/);
        await expect(picks.locator('[data-testid=pick-row][data-ticker="CRWV"]').getByTestId("pick-st")).toHaveClass(/text-neg-text/);
        await expect(picks.locator('[data-testid=pick-row][data-ticker="NBIS"]').getByTestId("pick-st")).toHaveText("—");
        await expect(picks).not.toContainText("×"); // no velocity on this card
        const sb = await q.getByTestId("pick-score").boundingBox();
        const st = await q.getByTestId("pick-st").boundingBox();
        if (width <= 400) expect(st!.y).toBeGreaterThan(sb!.y); // stacked under the score
        else expect(st!.x).toBeGreaterThan(sb!.x); // one line: score · ST
        await expectNoOverflow(page);
        await picks.screenshot({ path: `e2e/screenshots/universe-pick-${name}-${theme}.png` });
      });
    });
  }
}
