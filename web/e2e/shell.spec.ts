import { expect, test, type Page } from "@playwright/test";

import { expectNoOverflow, expectTouchTargets, isPhoneWidth, PHONE_75, PHONE_75_TAG } from "./mobile";

// TOWER_DESIGN §6: verified at 390x844, 768x1024 and 1440x900 in both themes.
const VIEWPORTS = [
  { name: "mobile", width: 390, height: 844, layout: "mobile" },
  PHONE_75, // E8.8a: the owner's iPhone at 75 % zoom (520x1125); phone-75 project only
  { name: "tablet", width: 768, height: 1024, layout: "mobile" }, // 768 is the §6 mobile upper bound
  { name: "desktop", width: 1440, height: 900, layout: "desktop" },
] as const;
const THEMES = ["dark", "light"] as const;
// Every component in TOWER_DESIGN §4 must have a story on /kitchen-sink.
const COMPONENTS = [
  "Shell", "StatCard", "ChangePill", "RangeControl", "TrendChart", "DivergingBars",
  "StackedBars", "ProportionBar", "ProgressRow", "Sparkline", "DataTable", "KeyValueList",
  "Timeline", "StatusStepper", "Section", "FilterBar", "EmptyState", "Tile", "DetailPanel",
  // E8.8a (TOWER_DESIGN §10).
  "Freshness", "InfoTip", "SegmentedControl", "CappedList",
];

async function open(page: Page, path: string, theme: (typeof THEMES)[number]) {
  await page.addInitScript((t) => localStorage.setItem("arc.theme", t), theme);
  await page.goto(path);
  await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
}

for (const vp of VIEWPORTS) {
  for (const theme of THEMES) {
    test.describe(`${vp.name} ${vp.width}x${vp.height} ${theme}`, { tag: vp.name === PHONE_75.name ? PHONE_75_TAG : [] }, () => {
      test.use({ viewport: { width: vp.width, height: vp.height } });

      test("shell renders", async ({ page }) => {
        await open(page, "/", theme);
        const shell = page.locator("[data-layout]");
        await expect(shell).toHaveAttribute("data-layout", vp.layout);
        await expect(page.getByRole("heading", { level: 1, name: "Overview" })).toBeVisible();
        await expect(page.getByTestId("theme-toggle")).toBeVisible();
        const nav = page.getByRole("navigation", { name: "Main" });
        for (const label of ["Overview", "Trades", "Positions", "Performance", "Ops"])
          await expect(nav.getByRole("link", { name: label })).toBeVisible();
        // Header as-of badge reads /api/snapshot: the scratch DB has no tick, so it is stale.
        await expect(page.locator("header [data-stale]")).toBeVisible();
        // No horizontal overflow at this width (page and every card; E8.8a helper).
        await expectNoOverflow(page);
        if (isPhoneWidth(page)) await expectTouchTargets(page, "header");
        await page.screenshot({ path: `e2e/screenshots/shell-${vp.name}-${theme}.png` });
      });

      test("kitchen sink renders every component", async ({ page }) => {
        await open(page, "/kitchen-sink", theme);
        for (const t of THEMES) await expect(page.getByTestId(`kitchen-${t}`)).toBeVisible();
        const names = (
          await page.getByTestId(`kitchen-${theme}`).locator("[data-story]").evaluateAll((els) =>
            els.map((e) => e.getAttribute("data-story") ?? ""),
          )
        ).join(" | ");
        for (const c of COMPONENTS) expect(names, `story for ${c}`).toContain(c);
        await expect(page.getByTestId("trend-chart").first().locator("svg path").first()).toBeVisible();
        await expectNoOverflow(page);
        await page.screenshot({ path: `e2e/screenshots/kitchen-sink-${vp.name}-${theme}.png`, fullPage: true });
      });
    });
  }
}

test("theme toggle persists in localStorage", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto("/");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await page.getByTestId("theme-toggle").click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
  expect(await page.evaluate(() => localStorage.getItem("arc.theme"))).toBe("light");
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
});

test("default theme follows prefers-color-scheme", async ({ page }) => {
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/");
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
});

test("tablet 1024 uses the icon rail", async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 768 });
  await page.goto("/");
  await expect(page.locator("[data-layout]")).toHaveAttribute("data-layout", "tablet");
  await page.screenshot({ path: "e2e/screenshots/shell-tablet-rail-1024.png" });
});

test("the SPA only ever issues GET requests", async ({ page }) => {
  const methods = new Set<string>();
  page.on("request", (r) => methods.add(r.method()));
  await page.goto("/");
  await page.goto("/kitchen-sink");
  await page.waitForLoadState("networkidle");
  expect([...methods]).toEqual(["GET"]);
});
