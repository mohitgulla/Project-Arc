import { expect, test, type Page } from "@playwright/test";

import { PHONE_75, PHONE_75_TAG } from "./mobile";

const HISTORY_URL =
  process.env.ARC_E2E_HISTORY_URL ?? `http://127.0.0.1:${process.env.ARC_E2E_HISTORY_PORT ?? "4183"}`;

// E8.7c Performance on the fixture DB with its performance history (~40 closed trades
// over 3+ months, anchored at server start): 90-day range vs the previous period at the
// TOWER_DESIGN §6 viewports x both themes, plus the controls and the empty state.
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

      test("renders every card for 90 days vs the previous period", async ({ page }) => {
        await open(page, "/performance?preset=90d&compare=prev", theme);
        for (const title of CARDS)
          await expect(page.getByRole("heading", { level: 2, name: title, exact: true })).toBeVisible();
        await expect(page.getByTestId("perf-range")).toContainText("–");
        await expect(page.getByText(/^vs -?\$[\d,]+\.\d\d in /)).toBeVisible();
        await expect(page.getByTestId("diverging-bars").locator("svg path").first()).toBeVisible();
        await expect(page.getByTestId("equity-drawdown-chart").locator("svg path").first()).toBeVisible();
        await expect(page.getByTestId("stacked-bars").locator("svg path").first()).toBeVisible();
        await expect(page.getByTestId("model-scatter").locator("svg .recharts-symbols").first()).toBeVisible();
        await expect(page.getByText("Sharpe (annualised)")).toBeVisible();
        await expect(page.getByText("Profit factor")).toBeVisible();
        await expect(page.getByTestId("breakdown-rows").locator(":scope > li").first()).toBeVisible();
        await expect(page.getByTestId("funnel").locator(":scope > li")).toHaveCount(5);
        await expect(page.getByTestId("violations")).toContainText("spread too wide");
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
        expect(overflow).toBeLessThanOrEqual(0);
        await page.screenshot({ path: `e2e/screenshots/performance-${vp.name}-${theme}.png`, fullPage: true });
      });
    });
  }
}

test.describe("performance behaviour", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("controls are URL-synced", async ({ page }) => {
    await open(page, "/performance");
    await page.getByLabel("Period").selectOption("ytd");
    await expect(page).toHaveURL(/preset=ytd/);
    await page.getByLabel("Compare").selectOption("none");
    await expect(page).toHaveURL(/compare=none/);
    await expect(page.getByText(/^vs .* in /)).toHaveCount(0);
    await page.getByLabel("Include paper test legs").click();
    await expect(page).toHaveURL(/include_tests=true/);
    await page.getByRole("tab", { name: "Regime at entry" }).click();
    await expect(page).toHaveURL(/by=regime/);
    await expect(page.getByTestId("breakdown-rows")).toContainText("Bull");
    await page.reload();
    await expect(page.getByLabel("Period")).toHaveValue("ytd");
    await expect(page.getByLabel("Include paper test legs")).toBeChecked();
    await expect(page.getByRole("tab", { name: "Regime at entry" })).toHaveAttribute("aria-selected", "true");
  });

  test("the shadow overlay toggles and a breakdown row opens the filtered Trades list", async ({ page }) => {
    await open(page, "/performance?preset=90d");
    const toggle = page.getByLabel(/Hold-to-expiry shadow/);
    await toggle.click(); // controlled by the URL: react-router applies it in a transition
    await expect(page).toHaveURL(/shadow=true/);
    await expect(toggle).toBeChecked();
    await page.getByTestId("breakdown-rows").locator("a").first().click();
    await expect(page).toHaveURL(/\/trades\?ticker=[A-Z]+&stage=closed&date=custom&date_from=/);
  });

  test("an empty period renders empty states, not zeros", async ({ page }) => {
    await open(page, "/performance?preset=custom&from=2020-01-01&to=2020-01-31&compare=none");
    await expect(page.getByText("No closed trades or equity closes in this period.")).toBeVisible();
    await expect(page.getByText("No trades closed in this period.")).toBeVisible();
    await expect(page.getByText("No fills in this period.")).toBeVisible();
    await expect(page.getByText("No proposals in this period.")).toBeVisible();
    await expect(page.getByTestId("diverging-bars")).toHaveCount(0);
  });
});
