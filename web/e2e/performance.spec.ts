import { expect, test, type Page } from "@playwright/test";

import { expectMinFontSize, expectNoOverflow, expectTouchTargets, isPhoneWidth, PHONE_75, PHONE_75_TAG } from "./mobile";

const HISTORY_URL =
  process.env.ARC_E2E_HISTORY_URL ?? `http://127.0.0.1:${process.env.ARC_E2E_HISTORY_PORT ?? "4183"}`;

// E8.7c / E8.8c Performance on the fixture DB with its performance history (~40 closed trades
// over 3+ months, anchored at server start): the default 3M range at the TOWER_DESIGN §6
// viewports plus phone-75 (520 px) x both themes, the range selector, and the empty state.
const VIEWPORTS = [
  { name: "mobile", width: 390, height: 844 },
  PHONE_75, // E8.8a: the owner's iPhone at 75 % zoom (520x1125); phone-75 project only
  { name: "tablet", width: 768, height: 1024 },
  { name: "desktop", width: 1440, height: 900 },
] as const;
const THEMES = ["dark", "light"] as const;
const CARDS = [
  "Net P&L",
  "Equity Curve",
  "Costs",
  "Win / Loss",
  "Modelled vs Realised",
  "Breakdowns",
  "Persona Calibration",
  "Gate & Funnel",
];

test.use({ baseURL: HISTORY_URL });

async function open(page: Page, path: string, theme: (typeof THEMES)[number] = "dark") {
  await page.addInitScript((t) => localStorage.setItem("arc.theme", t), theme);
  await page.goto(path);
  await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
  await expect(page.getByTestId("performance")).toBeVisible();
}

for (const vp of VIEWPORTS) {
  for (const theme of THEMES) {
    test.describe(`performance ${vp.name} ${vp.width}x${vp.height} ${theme}`, { tag: vp.name === PHONE_75.name ? PHONE_75_TAG : [] }, () => {
      test.use({ viewport: { width: vp.width, height: vp.height } });

      test("renders every card for the default 3M range", async ({ page }) => {
        const req = page.waitForRequest((r) => r.url().includes("/api/performance?"));
        await open(page, "/performance", theme);
        expect(Object.fromEntries(new URL((await req).url()).searchParams)).toEqual({ preset: "90d" });
        const range = page.getByTestId("perf-range-control");
        await expect(range.getByRole("tab", { name: "3M" })).toHaveAttribute("aria-selected", "true");
        for (const title of CARDS)
          await expect(page.getByRole("heading", { level: 2, name: title, exact: true })).toBeVisible();
        await expect(page.getByTestId("perf-range")).toContainText("–");
        await expect(page.getByText(/^vs -?\$/)).toHaveCount(0);
        await expect(page.getByTestId("diverging-bars").locator("svg path").first()).toBeVisible();
        await expect(page.getByTestId("equity-drawdown-chart").locator("svg path").first()).toBeVisible();
        await expect(page.getByTestId("stacked-bars").locator("svg path").first()).toBeVisible();
        await expect(page.getByTestId("model-scatter").locator("svg .recharts-symbols").first()).toBeVisible();
        await expect(page.getByText("Sharpe (annualised)")).toBeVisible();
        await expect(page.getByText("Profit factor")).toBeVisible();
        await expect(page.getByTestId("breakdown-rows").locator(":scope > *").first()).toBeVisible();
        await expect(page.getByTestId("funnel").locator(":scope > li")).toHaveCount(5);
        await expect(page.getByTestId("violations")).toContainText("spread too wide");
        // Every sub-text is one line (TOWER_DESIGN §10): its box is no taller than its line-height.
        expect(await page.locator("[data-subtext]").count()).toBeGreaterThanOrEqual(6);
        const tall = await page.evaluate(() =>
          Array.from(document.querySelectorAll<HTMLElement>("[data-subtext]"))
            .filter((el) => el.getBoundingClientRect().height > parseFloat(getComputedStyle(el).lineHeight) + 0.5)
            .map((el) => el.textContent),
        );
        expect(tall).toEqual([]);
        await expectNoOverflow(page);
        if (isPhoneWidth(page)) {
          // Breakdowns render as CardRows on mobile.
          await expect(page.getByTestId("breakdown-rows").locator("button").first()).toBeVisible();
          await expectTouchTargets(page, "[data-testid=perf-controls]");
          await expectTouchTargets(page, "[data-testid=breakdown-rows]");
          await expectMinFontSize(page, "main");
        }
        await page.screenshot({ path: `e2e/screenshots/performance-${vp.name}-${theme}.png`, fullPage: true });
      });
    });
  }
}

test.describe(`performance ${PHONE_75.name} sticky range`, { tag: PHONE_75_TAG }, () => {
  test("the range selector stays under the header while scrolling", async ({ page }) => {
    await open(page, "/performance");
    await expect(page.getByRole("heading", { level: 2, name: "Gate & Funnel", exact: true })).toBeVisible();
    await page.evaluate(() => window.scrollTo(0, 1500));
    await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(1000);
    const header = await page.locator("header").first().boundingBox();
    const box = await page.getByTestId("perf-controls").boundingBox();
    expect(Math.abs((box?.y ?? -99) - (header?.height ?? 0))).toBeLessThanOrEqual(1);
    await expect(page.getByTestId("perf-range-control").getByRole("tab", { name: "3M" })).toBeInViewport();
  });

  test("Win / Loss rows sit in two columns at 520 px", async ({ page }) => {
    await open(page, "/performance");
    const cols = page.getByTestId("kv-columns").locator(":scope > dl");
    await expect(cols).toHaveCount(2);
    const a = await cols.nth(0).boundingBox();
    const b = await cols.nth(1).boundingBox();
    expect(a?.y).toBe(b?.y);
    expect((b?.x ?? 0) > (a?.x ?? 0)).toBe(true);
  });

  test("a metric InfoTip opens on tap", async ({ page }) => {
    await open(page, "/performance");
    await page.getByRole("button", { name: "About Sharpe" }).tap();
    await expect(page.getByRole("tooltip")).toContainText("√252");
  });
});

test.describe("performance behaviour", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("the range selector is URL-synced and maps to the API preset", async ({ page }) => {
    await open(page, "/performance");
    await expect(page.getByLabel("Period")).toHaveCount(0);
    const range = page.getByTestId("perf-range-control");
    const req = page.waitForRequest((r) => r.url().includes("preset=ytd"));
    await range.getByRole("tab", { name: "YTD" }).click();
    await expect(page).toHaveURL(/range=YTD/);
    expect(Object.fromEntries(new URL((await req).url()).searchParams)).toEqual({ preset: "ytd" });
    await range.getByRole("tab", { name: "1W" }).click();
    await expect(page).toHaveURL(/range=1W/);
    await page.getByRole("tab", { name: "Regime at entry" }).click();
    await expect(page).toHaveURL(/by=regime/);
    await page.reload();
    await expect(page.getByTestId("perf-range-control").getByRole("tab", { name: "1W" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await expect(page.getByRole("tab", { name: "Regime at entry" })).toHaveAttribute("aria-selected", "true");
    await page.getByTestId("perf-range-control").getByRole("tab", { name: "3M" }).click();
    await expect(page.getByTestId("breakdown-rows")).toContainText("Bull");
  });

  test("the shadow overlay toggles and a breakdown row opens the filtered Trades list", async ({ page }) => {
    await open(page, "/performance?range=3M");
    const toggle = page.getByLabel(/Hold-to-expiry shadow/);
    await toggle.click(); // controlled by the URL: react-router applies it in a transition
    await expect(page).toHaveURL(/shadow=true/);
    await expect(toggle).toBeChecked();
    await page.getByTestId("breakdown-rows").locator("a").first().click();
    await expect(page).toHaveURL(/\/trades\?ticker=[A-Z]+&stage=closed&date=custom&date_from=/);
  });

  test("a range with no closed trades renders empty states, not zeros", async ({ page }) => {
    await open(page, "/performance?range=1D");
    await expect(page.getByTestId("perf-range-control").getByRole("tab", { name: "1D" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await expect(page.getByText("No trades closed in this period.")).toBeVisible();
  });
});
