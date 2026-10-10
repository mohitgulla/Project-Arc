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
        // D50: the env slot is plain text like the others, Title Case.
        await expect(page.getByTestId("env-slot")).toHaveText("Paper Trade");
        await expect(page.getByTestId("alerts-toggle")).toContainText("Alerts 1");
        await page.getByTestId("alerts-toggle").click();
        await expect(strip).toContainText("scalp slot 12:00 ET missed");
        // Cards.
        for (const title of ["Equity", "P&L Today", "Positions", "Greeks vs Caps", "Today's Proposals", "Today's Pick", "Movers", "Recent Activity · 24 h"])
          await expect(page.getByRole("heading", { level: 2, name: title, exact: false }).first()).toBeVisible();
        await expect(page.getByTestId("trend-chart").locator("svg path").first()).toBeVisible();
        await expect(page.getByTestId("mtd-ytd")).toContainText("MTD");
        await expect(page.getByText("Debit Vertical").first()).toBeVisible();
        // D50: direction right after the structure label (QQQ put debit vertical = Bearish).
        await expect(page.getByTestId("direction").first()).toBeVisible();
        await expect(page.locator("[data-direction=bearish]").first()).toHaveText("Bearish");
        await expect(page.getByText("NO", { exact: true }).or(page.getByText("not held")).first()).toBeVisible();
        await expect(page.getByTestId("proposals").locator(":scope > li")).toHaveCount(9);
        await expect(page.getByTestId("proposals")).toContainText("per_underlying_limit");
        // D59 / E14.10 (D67): Today's Pick = two stacked pill sections, Discovery then Trending,
        // headers `Tier (active)`; VIEW ALL opens Ops > Universe; no ST anywhere.
        const picks = page.getByTestId("picks");
        await expect(picks.getByTestId("pick-header")).toHaveCount(2);
        await expect(picks.getByTestId("pick-header").first()).toHaveText(/^Discovery \(\d+\)$/);
        await expect(picks.getByTestId("pick-st")).toHaveCount(0);
        await expect(picks).not.toContainText(/\d+% bull/);
        await expect(picks.getByRole("link", { name: /VIEW ALL/ })).toHaveAttribute("href", "/ops/universe");
        await expect(picks.getByTestId("pick-asof")).toBeVisible();
        // Column headers are caption size, smaller than the card title.
        const px = (l: import("@playwright/test").Locator) => l.evaluate((el) => parseFloat(getComputedStyle(el).fontSize));
        expect(await px(picks.getByTestId("pick-header").first())).toBeLessThan(await px(picks.getByRole("heading", { level: 2 }).first()));
        // Rolling 24 h, repeats grouped, capped at 8 with "Show n more".
        await expect(page.getByTestId("activity").locator(":scope > li")).toHaveCount(8);
        await expect(page.getByTestId("activity-more")).toHaveText("Show 1 more");
        await expect(page.getByTestId("activity")).toContainText("missed_window ×12");
        // D50: the range control sits below the hero block (under the comparison line), with dates.
        await expect(page.getByTestId("equity-range").getByRole("tab", { name: "3M" })).toBeVisible();
        const rangeBox = await page.getByTestId("equity-range").boundingBox();
        const cmpBox = await page.getByTestId("equity-comparison").boundingBox();
        expect(rangeBox!.y).toBeGreaterThanOrEqual(cmpBox!.y + cmpBox!.height);
        const chartBox = await page.getByTestId("trend-chart").boundingBox();
        expect(rangeBox!.y + rangeBox!.height).toBeLessThanOrEqual(chartBox!.y);
        await expect(page.getByTestId("equity-dates")).toHaveText(/^[A-Z][a-z]{2} \d{1,2}(, \d{4})?( – ([A-Z][a-z]{2} )?\d{1,2}, \d{4})?$/);
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

  test("range control switches the equity series; every range prints its dates", async ({ page }) => {
    await open(page, "/", "dark");
    await page.getByTestId("equity-info").click();
    await expect(page.getByText("Today's 10-min monitor marks.")).toBeVisible();
    await page.keyboard.press("Escape");
    const resp = page.waitForResponse((r) => r.url().includes("/api/overview?range=1M"));
    await page.getByRole("tab", { name: "1M" }).click();
    await resp;
    await expect(page).toHaveURL(/range=1M/);
    await page.getByTestId("equity-info").click();
    await expect(page.getByText("Reconciled daily closes.")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByText(/vs \$[\d,]+ at \d\d-\d\d/)).toBeVisible();
    const dates = page.getByTestId("equity-dates");
    for (const r of ["1W", "3M", "YTD", "ALL", "1D", "1M"]) {
      await page.getByRole("tab", { name: r, exact: true }).click();
      await expect(page).toHaveURL(new RegExp(`range=${r}`));
      await expect(dates).toHaveText(/\d{1,2}, \d{4}$/);
      if (r === "1M") await expect(dates).toContainText("–"); // a multi-day range spans two dates
    }
  });

  test("stale badge appears once the last monitor mark is older than 3x the cadence", async ({ page }) => {
    // The fixture's last mark is 2 min before server start; move the browser clock 45 min on
    // (past 3x the monitor cadence: the badge reads "stale after 30m" since the 10-min tick).
    await page.clock.install({ time: Date.now() + 45 * 60_000 });
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

// D50: slot order Trading · env · Health · Orders · Tick · Alerts (3x2 grid on a phone, one
// row on desktop). The fixture's active halt replaces slot 1 with the HALTED banner.
for (const vp of [PHONE_75, { name: "desktop", width: 1440, height: 900 }] as const) {
  test.describe(`overview status row order ${vp.name}`, { tag: vp.name === PHONE_75.name ? PHONE_75_TAG : [] }, () => {
    test.use({ viewport: { width: vp.width, height: vp.height } });
    test("slots read in the owner's order, Title Case", async ({ page }) => {
      await open(page, "/", "light");
      if (vp.name !== "desktop") {
        // the halt banner spans the grid; drop it to read the plain 3x2 slot grid
        await page.route("**/api/overview*", async (route) => {
          const res = await route.fetch();
          const body = await res.json();
          body.status.halted = false;
          body.status.halt = null;
          await route.fulfill({ response: res, json: body });
        });
        await page.reload();
        await expect(page.locator("[data-testid=status-slots] > li").first()).toHaveText("Trading Enabled");
      }
      await expect(page.locator("[data-testid=status-slots] > li")).toHaveCount(6);
      const texts = await page.locator("[data-testid=status-slots] > li").evaluateAll((lis) =>
        lis.map((li) => (li as HTMLElement).innerText.replace(/\s+/g, " ").replace(/ ?[▲▼]$/, "").trim()),
      );
      expect(texts).toHaveLength(6);
      expect(texts[0]).toMatch(vp.name === "desktop" ? /^HALTED / : /^Trading Enabled$/);
      expect(texts[1]).toBe("Paper Trade");
      expect(texts[2]).toMatch(/^Health (OK|Failed|Partial|No data)/);
      expect(texts[3]).toMatch(/^Orders 31\/200/);
      expect(texts[4]).toMatch(/^Tick /);
      expect(texts[5]).toBe("Alerts 1");
      // Visual reading order (top, then left) is the DOM order on both layouts. (The fixture's
      // long halt reason can wrap the desktop row; without a halt it is one row.)
      const boxes = await page.locator("[data-testid=status-slots] > li").evaluateAll((lis) =>
        lis.map((li, i) => ({ i, top: Math.round(li.getBoundingClientRect().top), left: Math.round(li.getBoundingClientRect().left) })),
      );
      const reading = [...boxes].sort((a, b) => a.top - b.top || a.left - b.left).map((b) => b.i);
      expect(reading).toEqual([0, 1, 2, 3, 4, 5]);
      if (vp.name !== "desktop") {
        // phone: 3x2 grid, row 1 Trading | env | Health, row 2 Orders | Tick | Alerts
        expect(new Set(boxes.slice(0, 3).map((b) => b.top)).size).toBe(1);
        expect(new Set(boxes.slice(3).map((b) => b.top)).size).toBe(1);
      }
    });
  });
}

test.describe("overview status row desktop without a halt", () => {
  test.use({ viewport: { width: 1440, height: 900 } });
  test("one row in the same order", async ({ page }) => {
    await page.route("**/api/overview*", async (route) => {
      const res = await route.fetch();
      const body = await res.json();
      body.status.halted = false;
      body.status.halt = null;
      await route.fulfill({ response: res, json: body });
    });
    await open(page, "/", "dark");
    const slots = page.locator("[data-testid=status-slots] > li");
    await expect(slots.first()).toHaveText("Trading Enabled");
    const tops = await slots.evaluateAll((lis) => lis.map((li) => Math.round(li.getBoundingClientRect().top)));
    expect(tops).toHaveLength(6);
    expect(new Set(tops).size).toBe(1);
    await page.getByTestId("status-strip").screenshot({ path: "e2e/screenshots/overview-status-row-desktop-dark.png" });
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
    const order = ["Equity", "P&L Today", "Positions", "Greeks vs Caps", "Today's Proposals", "Today's Pick", "Movers", "Recent Activity"];
    expect(order.map((o) => titles.findIndex((t) => t.startsWith(o)))).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
    const cols = await page.getByTestId("status-slots").evaluate((el) => getComputedStyle(el).gridTemplateColumns.split(" ").length);
    expect(cols).toBe(3);
  });
});
