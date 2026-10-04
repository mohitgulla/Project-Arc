import { expect, test, type Page } from "@playwright/test";

import { PHONE_75, PHONE_75_TAG } from "./mobile";

const FIXTURE_URL =
  process.env.ARC_E2E_FIXTURE_URL ?? `http://127.0.0.1:${process.env.ARC_E2E_FIXTURE_PORT ?? "4182"}`;

// E8.7b Trades list + drill-down on the populated fixture DB (scripts/tower_fixture_db.py):
// list with two URL filters, the detail page, mobile card rows + mobile detail, both themes.
const VIEWPORTS = [
  { name: "mobile", width: 390, height: 844 },
  PHONE_75, // E8.8a: the owner's iPhone at 75 % zoom (520x1125); phone-75 project only
  { name: "desktop", width: 1440, height: 900 },
] as const;
const THEMES = ["dark", "light"] as const;
const SECTIONS = [
  "sec-payoff",
  "sec-quant",
  "sec-decisions",
  "sec-gate",
  "sec-approval",
  "sec-execution",
  "sec-position",
  "sec-outcome",
  "sec-market",
  "sec-manifest",
];

test.use({ baseURL: FIXTURE_URL });

async function open(page: Page, path: string, theme: (typeof THEMES)[number]) {
  await page.addInitScript((t) => localStorage.setItem("arc.theme", t), theme);
  await page.goto(path);
  await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
  await expect(page.getByTestId("trades")).toBeVisible();
}

async function noOverflow(page: Page) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
}

for (const vp of VIEWPORTS) {
  for (const theme of THEMES) {
    test.describe(`trades ${vp.name} ${vp.width}x${vp.height} ${theme}`, { tag: vp.name === PHONE_75.name ? PHONE_75_TAG : [] }, () => {
      test.use({ viewport: { width: vp.width, height: vp.height } });

      test("list with two URL filters, then the SPY drill-down", async ({ page }) => {
        const errors: string[] = [];
        // The shell ships no favicon (pre-existing, not this card): ignore that one 404.
        const noise = (s: string) => s.includes("/favicon.ico");
        page.on("pageerror", (e) => errors.push(e.message));
        page.on("console", (m) => {
          const s = `${m.text()} ${m.location().url}`;
          if (m.type() === "error" && !noise(s)) errors.push(s);
        });
        page.on("response", (r) => {
          if (r.status() >= 400 && !noise(r.url())) errors.push(`${r.status()} ${r.url()}`);
        });
        await open(page, "/trades?stage=open,closed&kind=open", theme);
        await expect(page.getByTestId("summary-count")).toContainText("4");
        await expect(page.getByTestId("summary-realized")).toContainText("+$300.00");
        const mobile = vp.width <= 768; // §6 mobile layout (390, phone-75 520, 768)
        const rows = mobile ? page.getByTestId("datatable-cards").locator(":scope > button") : page.locator("tbody tr");
        await expect(rows).toHaveCount(4);
        if (mobile) await expect(page.getByTestId("filters-open")).toContainText("Filters (3)");
        await noOverflow(page);
        await page.screenshot({ path: `e2e/screenshots/trades-${vp.name}-${theme}.png`, fullPage: true });

        await rows.filter({ hasText: "SPY" }).first().click();
        await expect(page).toHaveURL(/\/trades\/[0-9a-f]{64}\?stage=open%2Cclosed&kind=open$|\/trades\/[0-9a-f]{64}\?stage=open,closed&kind=open$/);
        const detail = page.getByTestId("trade-detail");
        await expect(detail).toBeVisible();
        await expect(page.getByRole("complementary", { name: "Detail" })).toHaveAttribute("data-mode", vp.name === "desktop" ? "panel" : "page");
        for (const s of SECTIONS) await expect(page.getByTestId(s)).toBeVisible();
        await expect(page.getByTestId("payoff-chart").locator("svg path").first()).toBeVisible();
        await expect(page.getByTestId("sec-gate")).toContainText("PASS");
        await expect(page.getByTestId("sec-approval")).toContainText("U0OWNER001");
        await expect(page.getByTestId("order-events")).toContainText("submitted → filled");
        await expect(page.getByTestId("sec-market")).toContainText("snap-fx-spy");
        await expect(page.getByTestId("sec-manifest")).toContainText("0123456789ab");
        await expect(page.getByTestId("sec-decisions")).toContainText("Risk reviewed it");
        await expect(page.getByTestId("sec-decisions")).not.toContainText("XLU");
        await page.screenshot({ path: `e2e/screenshots/trade-detail-${vp.name}-${theme}.png`, fullPage: true });
        // Expand a decision's details (and its persona call) too: still no errors.
        await page.getByTestId("sec-decisions").getByRole("button", { name: "▶ Details" }).nth(4).click();
        await expect(page.getByTestId("persona-call").first()).toContainText("claude-");
        expect(errors).toEqual([]);
        if (mobile) {
          await page.getByRole("button", { name: "Back" }).click();
          await expect(page).toHaveURL(/\/trades\?stage=/);
        }
      });
    });
  }
}

test.describe("trades behaviour", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("gate FAIL shows both violations with plain labels", async ({ page }) => {
    await open(page, "/trades?stage=gate_fail&ticker=AAPL", "dark");
    await page.locator("tbody tr").first().click();
    const v = page.getByTestId("gate-violation");
    await expect(v).toHaveCount(2);
    await expect(v.first()).toContainText("Gate: over the per-underlying limit");
    await expect(v.nth(1)).toContainText("Gate: bid-ask spread too wide");
  });

  test("close-to-reallocate pair links both ways", async ({ page }) => {
    await open(page, "/trades?ticker=AMD&kind=open", "light");
    await page.locator("tbody tr").first().click();
    await expect(page.getByTestId("swaps")).toContainText("AMD");
    await expect(page.getByTestId("swaps")).toContainText("XLE");
    await expect(page.getByTestId("review")).toContainText("Good decision good outcome");
    await page.getByTestId("exit-link").getByRole("link").click();
    await expect(page.getByTestId("trade-header")).toContainText("close");
    await expect(page.getByTestId("sec-decisions")).toContainText("Exit: close to reallocate");
    await page.screenshot({ path: "e2e/screenshots/trade-detail-close-desktop-light.png", fullPage: true });
  });

  test("sort, paging and clear stay in the URL", async ({ page }) => {
    await open(page, "/trades?size=5", "dark");
    await expect(page.getByTestId("pager")).toContainText("1–5 of 15");
    await page.getByRole("button", { name: "Next ›" }).click();
    await expect(page).toHaveURL(/page=2/);
    await expect(page.getByTestId("pager")).toContainText("6–10 of 15");
    await page.getByRole("button", { name: /Net EV/ }).click();
    await expect(page).toHaveURL(/sort=net_ev/);
    await expect(page).not.toHaveURL(/page=2/);
    await page.getByLabel("Stage").selectOption("gate_fail");
    await expect(page.getByTestId("summary-count")).toContainText("2");
    await page.getByTestId("filters-clear").click();
    await expect(page.getByTestId("summary-count")).toContainText("15");
  });

  test("global search jumps to a ticker and to a run's trade", async ({ page }) => {
    await open(page, "/trades", "dark");
    const box = page.getByTestId("global-search").getByRole("searchbox").first();
    await box.fill("run-fx-risk");
    await expect(page.getByRole("listbox", { name: "Search results" })).toContainText("SPY");
    await box.press("Enter");
    await expect(page).toHaveURL(/\/trades\/[0-9a-f]{64}$/);
    await expect(page.getByTestId("trade-header")).toContainText("SPY");
    await box.fill("XLE");
    await box.press("Enter");
    await expect(page).toHaveURL(/\/trades\?ticker=XLE$/);
    await expect(page.getByTestId("summary-count")).toContainText("1");
  });

  test("unknown hash shows an empty state", async ({ page }) => {
    await open(page, `/trades/${"0".repeat(64)}`, "dark");
    await expect(page.getByTestId("trades")).toContainText("No trade 000000000000");
  });
});
