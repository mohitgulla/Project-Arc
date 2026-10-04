import { expect, test, type Page } from "@playwright/test";

import { PHONE_75, PHONE_75_TAG } from "./mobile";

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
        await expect(page.getByTestId("order-budget")).toContainText("31/200");
        await page.getByTestId("alerts-toggle").click();
        await expect(strip).toContainText("scout slot 12:00 ET missed");
        // Cards.
        for (const title of ["Equity", "P&L Today", "Positions", "Greeks vs Caps", "Today's Proposals", "Movers", "Recent Activity"])
          await expect(page.getByRole("heading", { level: 2, name: title, exact: false }).first()).toBeVisible();
        await expect(page.getByTestId("trend-chart").locator("svg path").first()).toBeVisible();
        await expect(page.getByTestId("mtd-ytd")).toContainText("MTD");
        await expect(page.getByText("Debit vertical").first()).toBeVisible();
        await expect(page.getByText("NO", { exact: true }).or(page.getByText("not held")).first()).toBeVisible();
        await expect(page.getByTestId("proposals").locator(":scope > li")).toHaveCount(9);
        await expect(page.getByTestId("proposals")).toContainText("per_underlying_limit");
        await expect(page.getByTestId("activity").locator(":scope > li")).toHaveCount(20);
        await expect(page.getByTestId("max-loss-caps")).toContainText("SPY");
        // Fresh marks: the Greeks header badge is fresh (one freshness slot, §10).
        await expect(page.getByTestId("greeks-freshness")).toHaveAttribute("data-freshness", "fresh");
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
        expect(overflow).toBeLessThanOrEqual(0);
        await page.screenshot({ path: `e2e/screenshots/overview-${vp.name}-${theme}.png`, fullPage: true });
      });
    });
  }
}

test.describe("overview behaviour", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("range control switches the equity series", async ({ page }) => {
    await open(page, "/", "dark");
    await expect(page.getByText("Today's 5-min monitor marks")).toBeVisible();
    const resp = page.waitForResponse((r) => r.url().includes("/api/overview?range=1M"));
    await page.getByRole("tab", { name: "1M" }).click();
    await resp;
    await expect(page).toHaveURL(/range=1M/);
    await expect(page.getByText("Reconciled daily closes")).toBeVisible();
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
});
