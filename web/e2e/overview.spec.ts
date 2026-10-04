import { expect, test, type Page } from "@playwright/test";

import { PHONE_75, PHONE_75_TAG, expectMinFontSize, expectNoOverflow, expectTouchTargets, isPhoneWidth } from "./mobile";

const FIXTURE_URL =
  process.env.ARC_E2E_FIXTURE_URL ?? `http://127.0.0.1:${process.env.ARC_E2E_FIXTURE_PORT ?? "4182"}`;

// E8.7a Overview on the populated fixture DB (scripts/tower_fixture_db.py, anchored at
// server start): TOWER_DESIGN §6 viewports x both themes, plus the stale badge both ways.
const VIEWPORTS = [
  { name: "mobile", width: 390, height: 844 },
  PHONE_75, // E8.8a: the owner's iPhone at 75 % zoom (520x1125); phone-75 project only
  { name: "tablet", width: 768, height: 1024 },
  { name: "desktop", width: 1440, height: 900 },
] as const;
const THEMES = ["dark", "light"] as const;

test.use({ baseURL: FIXTURE_URL });

async function open(page: Page, path: string, theme: (typeof THEMES)[number]) {
  await page.addInitScript((t) => localStorage.setItem("arc.theme", t), theme);
  await page.goto(path);
  await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
  await expect(page.getByTestId("overview")).toBeVisible();
}

for (const vp of VIEWPORTS) {
  for (const theme of THEMES) {
    test.describe(`overview ${vp.name} ${vp.width}x${vp.height} ${theme}`, { tag: vp.name === PHONE_75.name ? PHONE_75_TAG : [] }, () => {
      test.use({ viewport: { width: vp.width, height: vp.height } });

      test("renders every card on the fixture", async ({ page }) => {
        await open(page, "/", theme);
        // Status strip: the fixture has one active halt, one open alert and the D32 budget.
        const strip = page.getByTestId("status-strip");
        await expect(strip).toHaveAttribute("data-tone", "neg");
        await expect(page.getByTestId("halt-banner")).toContainText("HALTED");
        await expect(page.getByTestId("halt-banner")).toContainText("arc:reconcile");
        // E8.8b status row: fixed slots, `Orders n/limit` (never "Orders today").
        await expect(page.getByTestId("order-budget")).toHaveText(/^Orders 31\/200/);
        await expect(strip).not.toContainText("Orders today");
        await expect(page.getByTestId("slot-tick")).toContainText("Tick");
        await expect(page.getByTestId("slot-health")).toContainText("Health");
        await expect(page.getByTestId("env-chip")).toContainText("paper");
        await expect(page.getByTestId("alerts-toggle")).toContainText("Alerts 1");
        await page.getByTestId("alerts-toggle").click();
        await expect(strip).toContainText("scout slot 12:00 ET missed");
        // Cards.
        for (const title of ["Equity", "P&L Today", "Positions", "Greeks vs Caps", "Today's Proposals", "Movers", "Recent Activity · 24 h"])
          await expect(page.getByRole("heading", { level: 2, name: title, exact: false }).first()).toBeVisible();
        await expect(page.getByTestId("trend-chart").locator("svg path").first()).toBeVisible();
        await expect(page.getByTestId("mtd-ytd")).toContainText("MTD");
        await expect(page.getByText("Debit vertical").first()).toBeVisible();
        await expect(page.getByText("NO", { exact: true }).or(page.getByText("not held")).first()).toBeVisible();
        await expect(page.getByTestId("proposals").locator(":scope > li")).toHaveCount(9);
        await expect(page.getByTestId("proposals")).toContainText("per_underlying_limit");
        // Rolling 24 h, repeats grouped, capped at 8 with "Show n more".
        await expect(page.getByTestId("activity").locator(":scope > li")).toHaveCount(8);
        await expect(page.getByTestId("activity-more")).toHaveText("Show 1 more");
        await expect(page.getByTestId("activity")).toContainText("missed_window ×12");
        // Range control lives in the Equity card header, above the hero number.
        const header = page.getByTestId("equity-range").locator("xpath=ancestor::header[1]");
        await expect(header.getByRole("tab", { name: "3M" })).toBeVisible();
        const rangeBox = await page.getByTestId("equity-range").boundingBox();
        const heroBox = await page.locator("section:has([data-testid=equity-range]) .text-hero").boundingBox();
        expect(rangeBox!.y + rangeBox!.height).toBeLessThanOrEqual(heroBox!.y);
        await expect(page.getByTestId("max-loss-caps")).toContainText("SPY");
        // Fresh marks: the Greeks header badge is fresh (one freshness slot, §10).
        await expect(page.getByTestId("greeks-freshness")).toHaveAttribute("data-freshness", "fresh");
        await expectNoOverflow(page);
        if (isPhoneWidth(page)) {
          await expectTouchTargets(page, "[data-testid=overview]");
          await expectMinFontSize(page, "[data-testid=overview]");
        }
        await page.screenshot({ path: `e2e/screenshots/overview-${vp.name}-${theme}.png`, fullPage: true });
      });
    });
  }
}

test.describe("overview behaviour", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("range control switches the equity series", async ({ page }) => {
    await open(page, "/", "dark");
    await page.getByTestId("equity-info").click();
    await expect(page.getByText("Today's 5-min monitor marks.")).toBeVisible();
    await page.keyboard.press("Escape");
    const resp = page.waitForResponse((r) => r.url().includes("/api/overview?range=1M"));
    await page.getByRole("tab", { name: "1M" }).click();
    await resp;
    await expect(page).toHaveURL(/range=1M/);
    await page.getByTestId("equity-info").click();
    await expect(page.getByText("Reconciled daily closes.")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByText(/vs \$[\d,]+ at \d\d-\d\d/)).toBeVisible();
  });

  test("stale badge appears once the last monitor mark is older than 3x the cadence", async ({ page }) => {
    // The fixture's last mark is 2 min before server start; move the browser clock 20 min on.
    await page.clock.install({ time: Date.now() + 20 * 60_000 });
    await open(page, "/", "light");
    await expect(page.getByTestId("greeks-freshness")).toHaveAttribute("data-freshness", "stale");
    await expect(page.getByTestId("greeks-freshness")).toContainText("stale ·");
    await expect(page.locator("[data-stale=true]").first()).toBeVisible();
    await page.screenshot({ path: "e2e/screenshots/overview-stale-desktop-light.png", fullPage: true });
  });

  test("position row links to its trade; VIEW ALL opens the positions page", async ({ page }) => {
    await open(page, "/", "dark");
    await page.getByRole("row").filter({ hasText: "SPY" }).first().click();
    await expect(page).toHaveURL(/\/trades\/[0-9a-f]{64}$/);
    await page.goto("/positions?status=closed");
    await expect(page.getByRole("row").filter({ hasText: "AMD" })).toBeVisible();
    await page.getByRole("tab", { name: "all" }).click();
    await expect(page.getByRole("row")).toHaveCount(5); // header + 4
    await page.screenshot({ path: "e2e/screenshots/positions-desktop-dark.png", fullPage: true });
  });

  test("grouped alerts expand; VIEW ALL goes to Ops alerts", async ({ page }) => {
    await open(page, "/", "dark");
    const group = page.locator("[data-group=missed_window] > button");
    await group.click();
    await expect(page.getByTestId("activity-group").locator("li")).toHaveCount(12);
    await page.getByTestId("activity-more").click();
    await expect(page.getByTestId("activity").locator(":scope > li")).toHaveCount(9);
    await page.getByTestId("activity-card").getByRole("link", { name: /VIEW ALL/ }).click();
    await expect(page).toHaveURL(/\/ops#alerts$/);
  });
});

test.describe("overview phone-75 layout", { tag: PHONE_75_TAG }, () => {
  test("single-column order and a 3x2 status grid", async ({ page }) => {
    await open(page, "/", "dark");
    const titles = await page.locator("[data-testid=overview-grid] h2").evaluateAll((hs) =>
      hs
        .map((h) => ({ t: h.textContent?.trim() ?? "", y: h.getBoundingClientRect().top }))
        .sort((a, b) => a.y - b.y)
        .map((x) => x.t),
    );
    const order = ["Equity", "P&L Today", "Positions", "Greeks vs Caps", "Today's Proposals", "Movers", "Recent Activity"];
    expect(order.map((o) => titles.findIndex((t) => t.startsWith(o)))).toEqual([0, 1, 2, 3, 4, 5, 6]);
    const cols = await page.getByTestId("status-slots").evaluate((el) => getComputedStyle(el).gridTemplateColumns.split(" ").length);
    expect(cols).toBe(3);
  });
});
