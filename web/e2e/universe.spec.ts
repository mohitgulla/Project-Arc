import { expect, test, type Locator, type Page } from "@playwright/test";

import { expectMinFontSize, expectNoOverflow, expectTouchTargets, PHONE_75, PHONE_75_TAG, viewportUse } from "./mobile";

const OPS_URL = process.env.ARC_E2E_OPS_URL ?? `http://127.0.0.1:${process.env.ARC_E2E_OPS_PORT ?? "4184"}`;

// E12.6 (D56 since E13.15): the Universe page on the --ops fixture
// (scripts/tower_fixture_ops.py add_universe: core 20 + momentum 11 + discovery 19 active,
// 4 momentum names past the tier size and 1 discovery name past the cap, a partial momentum
// feed and the 100-name pre-D51 `universe` override, which is ignored). E14.8 (D64): discovery
// carries two-run scores (NBIS carried) and three Stocktwits readings (QCOM 80 % bull, CRWV 30 %,
// NBIS too few tags). E14.10 (D67): every tier is a 4-column pill grid, details behind a click,
// and the Stocktwits readings are shown nowhere.
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

/** 4 equal-width columns: same width everywhere, same x per column across rows, 4 distinct x. */
async function expectFourColumns(pills: Locator) {
  const boxes = (await pills.evaluateAll((els) => els.map((e) => e.getBoundingClientRect()).map((r) => ({ x: r.x, w: r.width, h: r.height })))) as {
    x: number;
    w: number;
    h: number;
  }[];
  expect(boxes.length).toBeGreaterThanOrEqual(5);
  const w0 = boxes[0]!.w;
  for (const b of boxes) {
    expect(Math.abs(b.w - w0)).toBeLessThanOrEqual(0.5);
    expect(b.h).toBe(boxes[0]!.h);
  }
  const xs = [...new Set(boxes.map((b) => Math.round(b.x)))];
  expect(xs).toHaveLength(4);
  boxes.forEach((b, i) => expect(Math.round(b.x)).toBe(Math.round(boxes[i % 4]!.x)));
}

async function expectNoSentiment(page: Page) {
  await expect(page.getByTestId("uni-st")).toHaveCount(0);
  await expect(page.getByTestId("pick-st")).toHaveCount(0);
  expect(await page.locator("body").innerText()).not.toMatch(/\d+% bull/);
}

for (const vp of VIEWPORTS) {
  for (const theme of THEMES) {
    test.describe(`universe ${vp.name} ${vp.width}x${vp.height} ${theme}`, { tag: vp.name === PHONE_75.name ? PHONE_75_TAG : [] }, () => {
      test.use(viewportUse(vp));

      test("renders the summary, tier pill grids in order and the reference line", async ({ page }) => {
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
        await expect(core.getByTestId("uni-pill")).toHaveCount(20);
        await expect(core.getByTestId("uni-tier-counts")).toHaveText("20 names");
        await expectFourColumns(core.getByTestId("uni-pill"));
        // core / momentum pills carry no score; discovery pills do (muted micro)
        await expect(core.getByTestId("pill-score")).toHaveCount(0);
        const mom = root.locator('[data-testid=uni-tier][data-tier="momentum"]');
        await expect(mom.getByTestId("uni-pill")).toHaveCount(11);
        await expect(mom.getByTestId("uni-partial")).toHaveText("Partial");
        await expect(mom.getByTestId("uni-tier-counts")).toHaveText("top 20 of 24 listed");
        await expect(mom.getByTestId("uni-tier-active")).toHaveText("active 11 / 20");
        await expect(root).not.toContainText("offered");
        await expect(mom.getByTestId("uni-tier-meta")).toContainText("source stockanalysis");
        await expect(mom.getByTestId("uni-tier-meta")).toContainText("refreshed 3d ago");
        const disc = root.locator('[data-testid=uni-tier][data-tier="discovery"]');
        await expect(disc.getByTestId("uni-tier-counts")).toHaveText("20 names (1 carried)");
        await expectFourColumns(disc.getByTestId("uni-pill"));
        const qcom = disc.locator('[data-testid=uni-pill][data-ticker="QCOM"]');
        await expect(qcom.getByTestId("pill-score")).toHaveText("0.86");
        const nbis = disc.locator('[data-testid=uni-pill][data-ticker="NBIS"]');
        await expect(nbis.getByTestId("pill-carried")).toHaveAttribute("title", /^Carried from \d{4}-\d{2}-\d{2}$/);
        // AAPL is in core and momentum: the also-in dot explains it
        await expect(core.locator('[data-testid=uni-pill][data-ticker="AAPL"]').getByTestId("pill-also")).toHaveAttribute("title", "Also in Momentum");
        const trend = root.locator('[data-testid=uni-tier][data-tier="trending"]');
        await expect(trend).toContainText("No names in this tier today.");
        await expect(root.getByTestId("uni-expired")).toHaveCount(0);
        // nothing open by default; market reference stays a visible line
        await expect(root.getByTestId("uni-pill-detail")).toHaveCount(0);
        await expect(root.getByTestId("uni-market-ref-line")).toHaveText("SPY QQQ IWM (regime only, not traded)");
        await expect(root.getByTestId("uni-override-ignored")).toContainText("100 names");
        await expectNoSentiment(page);
        await expectNoOverflow(page);
        if (vp.width <= 520) {
          await expectTouchTargets(page, "[data-testid=uni-grid]");
          await expectMinFontSize(page, "[data-testid=ops-universe]");
        }
        await page.screenshot({ path: `e2e/screenshots/universe-${vp.name}-${theme}.png`, fullPage: true });
      });

      test("dropped, tail cuts and the discovery fill sit in one closed disclosure", async ({ page }) => {
        await open(page, "/ops/universe", theme);
        const dis = page.getByTestId("uni-dropped-ref");
        await expect(dis.locator("summary")).toContainText("Dropped & Reference");
        await expect(dis.locator("summary")).toContainText("(5)");
        await expect(page.getByTestId("uni-drop")).toHaveCount(0); // closed by default
        await dis.locator("summary").click();
        const drops = page.getByTestId("uni-drop");
        await expect(drops).toHaveCount(5);
        await expect(drops.first()).toContainText("KKR");
        await expect(drops.first()).toContainText("Momentum");
        await expect(drops.first()).toContainText("past the tier's size");
        await expect(drops.last()).toContainText("BBAI");
        await expect(drops.last()).toContainText("past the active-list cap");
      });

      test("a pill opens its detail panel below its row; second click and Esc close it", async ({ page }) => {
        await open(page, "/ops/universe", theme);
        const qcom = page.locator('[data-testid=uni-pill][data-ticker="QCOM"]');
        await expect(qcom).toHaveAttribute("aria-expanded", "false");
        await qcom.click();
        await expect(qcom).toHaveAttribute("aria-expanded", "true");
        await expect(page).toHaveURL(/\/ops\/universe\?t=QCOM$/);
        const d = page.locator('[data-testid=uni-pill-detail][data-ticker="QCOM"]');
        await expect(d.locator("[data-fact=rank] dd")).toHaveText("#1 in Discovery");
        await expect(d.locator("[data-fact=score] dd")).toHaveText("0.86");
        await expect(d.locator("[data-fact=today-prev] dd")).toHaveText("0.86 / 0.70");
        await expect(d.locator("[data-fact=sources] dd")).toHaveText("Arete Trading, FX Evolution");
        await expect(d.locator("[data-fact=stance] dd")).toHaveText("Bullish");
        await expect(d).toContainText(/In tier \d+ of the last 20 sessions/);
        await expect(d).toContainText("YouTube call"); // the reason
        await expect(d).not.toContainText("Stocktwits");
        // full width, directly below the pill's grid row
        const pb = (await qcom.boundingBox())!;
        const db = (await d.boundingBox())!;
        const grid = (await page.locator('[data-testid=uni-tier][data-tier="discovery"] [data-testid=uni-grid]').boundingBox())!;
        expect(db.y).toBeGreaterThanOrEqual(pb.y + pb.height);
        expect(db.y - (pb.y + pb.height)).toBeLessThan(16);
        expect(Math.abs(db.width - grid.width)).toBeLessThanOrEqual(1);
        // accordion: opening another closes QCOM
        await page.locator('[data-testid=uni-pill][data-ticker="CRWV"]').click();
        await expect(page.getByTestId("uni-pill-detail")).toHaveCount(1);
        await expect(page.locator('[data-testid=uni-pill-detail][data-ticker="CRWV"] [data-fact=stance] dd')).toHaveText("Bearish");
        await page.locator('[data-testid=uni-pill][data-ticker="CRWV"]').click();
        await expect(page.getByTestId("uni-pill-detail")).toHaveCount(0);
        // momentum shows the SPMO weight; core the also-in line
        await page.locator('[data-testid=uni-tier][data-tier="momentum"] [data-testid=uni-pill]').first().click();
        await expect(page.locator("[data-testid=uni-pill-detail] [data-fact=weight] dd")).toHaveText(/^\d+\.\d{2}%$/);
        await page.keyboard.press("Escape");
        await expect(page.getByTestId("uni-pill-detail")).toHaveCount(0);
        await page.locator('[data-testid=uni-tier][data-tier="core"] [data-testid=uni-pill][data-ticker="AAPL"]').click();
        const a = page.locator('[data-testid=uni-pill-detail][data-ticker="AAPL"]');
        await expect(a).toContainText("core list");
        await expect(a).toContainText("also in Momentum");
        await expectNoOverflow(page);
        await page.screenshot({ path: `e2e/screenshots/universe-open-${vp.name}-${theme}.png`, fullPage: true });
      });

      test("?t= deep link opens and scrolls to that pill's panel", async ({ page }) => {
        await open(page, "/ops/universe?t=nbis", theme);
        const d = page.locator('[data-testid=uni-pill-detail][data-ticker="NBIS"]');
        await expect(d).toBeVisible();
        await expect(d).toContainText(/carried from \d{4}-\d{2}-\d{2}/);
        await expect(d.locator("[data-fact=today-prev] dd")).toHaveText("— / 0.70");
        await expect(page.locator('[data-testid=uni-pill][data-ticker="NBIS"]')).toBeInViewport();
        await expectNoSentiment(page);
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

// E14.10 (D67): Today's Pick (Overview) on the same store, at 360 / 520 / 768 / 1440: stacked
// Discovery then Trending pill grids (4 columns at every width), ticker + score, at most 12 then
// `+k more`; a pill opens its Ops › Universe panel; no ST.
for (const { name, width } of [
  { name: "w360", width: 360 },
  { name: "w520", width: 520 },
  { name: "w768", width: 768 },
  { name: "w1440", width: 1440 },
]) {
  for (const theme of THEMES) {
    test.describe(`today's pick ${name} ${theme}`, () => {
      test.use({ viewport: { width, height: 900 } });
      test("Today's Pick shows 4-column pill grids with scores, no ST", async ({ page }) => {
        await open(page, "/", theme);
        const picks = page.getByTestId("picks");
        const headers = picks.getByTestId("pick-header");
        await expect(headers).toHaveText(["Discovery (19)", "Trending (0)"]);
        const disc = picks.locator('[data-testid=pick-tier][data-tier="discovery"]');
        await expect(disc.getByTestId("pick-pill")).toHaveCount(12);
        await expect(disc.getByTestId("pick-more")).toHaveText(/^\+7 more$/i);
        await expect(disc.getByTestId("pick-more")).toHaveAttribute("href", "/ops/universe");
        await expectFourColumns(disc.getByTestId("pick-pill"));
        const q = disc.locator('[data-testid=pick-pill][data-ticker="QCOM"]');
        await expect(q.getByTestId("pill-score")).toHaveText("0.86");
        await expect(q).toHaveAttribute("href", "/ops/universe?t=QCOM");
        await expect(picks.locator('[data-testid=pick-tier][data-tier="trending"]')).toContainText("None today");
        await expect(picks).not.toContainText("score · ST");
        await expect(picks.getByTestId("pick-legend")).toHaveCount(0);
        await expect(picks.getByRole("link", { name: /VIEW ALL/ })).toHaveAttribute("href", "/ops/universe");
        await expectNoSentiment(page);
        await expectNoOverflow(page);
        await picks.screenshot({ path: `e2e/screenshots/universe-pick-${name}-${theme}.png` });
      });
    });
  }
}

test("a Today's Pick pill opens that name's Universe panel", async ({ page }) => {
  await open(page, "/");
  await page.getByTestId("picks").locator('[data-testid=pick-pill][data-ticker="CRWV"]').click();
  await expect(page).toHaveURL(/\/ops\/universe\?t=CRWV$/);
  const d = page.locator('[data-testid=uni-pill-detail][data-ticker="CRWV"]');
  await expect(d.locator("[data-fact=rank] dd")).toHaveText("#2 in Discovery");
  await expect(d).toContainText("YouTube call");
});
