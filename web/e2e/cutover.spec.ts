import { expect, test, type Page } from "@playwright/test";

import { PHONE_75, PHONE_75_TAG } from "./mobile";

// E8.7e cutover smoke: `arc tower serve` (no flag, uvicorn) serves all five pages of the
// v2 tower at the phone and desktop viewports in both themes, against the fixture DB
// with its Ops rows (e2e/serve.mjs --ops). Screenshots: e2e/screenshots/cutover-*.png.
const OPS_URL = process.env.ARC_E2E_OPS_URL ?? `http://127.0.0.1:${process.env.ARC_E2E_OPS_PORT ?? "4184"}`;
const VIEWPORTS = [
  { name: "mobile", width: 390, height: 844 },
  PHONE_75, // E8.8a: the owner's iPhone at 75 % zoom (520x1125); phone-75 project only
  { name: "desktop", width: 1440, height: 900 },
] as const;
const THEMES = ["dark", "light"] as const;
const PAGES = [
  { path: "/", title: "Overview", slug: "overview" },
  { path: "/trades", title: "Trades", slug: "trades" },
  { path: "/positions", title: "Positions", slug: "positions" },
  { path: "/performance", title: "Performance", slug: "performance" },
  { path: "/ops", title: "Ops", slug: "ops" },
] as const;

test.use({ baseURL: OPS_URL });

async function open(page: Page, path: string, theme: (typeof THEMES)[number]) {
  await page.addInitScript((t) => localStorage.setItem("arc.theme", t), theme);
  await page.goto(path);
  await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
}

for (const vp of VIEWPORTS) {
  for (const theme of THEMES) {
    test.describe(`cutover ${vp.name} ${vp.width}x${vp.height} ${theme}`, { tag: vp.name === PHONE_75.name ? PHONE_75_TAG : [] }, () => {
      test.use({ viewport: { width: vp.width, height: vp.height } });

      for (const pg of PAGES) {
        test(`${pg.slug} renders`, async ({ page }) => {
          const errors: string[] = [];
          page.on("pageerror", (e) => errors.push(e.message));
          await open(page, pg.path, theme);
          await expect(page.getByRole("heading", { level: 1, name: pg.title })).toBeVisible();
          const nav = page.getByRole("navigation", { name: "Main" });
          await expect(nav.getByRole("link", { name: pg.title })).toBeVisible();
          // Nothing failed to load: no error card from the API on a healthy fixture store.
          await expect(page.getByText("db_unavailable")).toHaveCount(0);
          await page.waitForLoadState("networkidle");
          const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
          expect(overflow).toBeLessThanOrEqual(0);
          expect(errors).toEqual([]);
          await page.screenshot({
            path: `e2e/screenshots/cutover-${pg.slug}-${vp.name}-${theme}.png`,
            fullPage: true,
          });
        });
      }
    });
  }
}

test("the tower is GET-only and answers /api/health", async ({ request }) => {
  const health = await request.get("/api/health");
  expect(health.status()).toBe(200);
  expect((await health.json()).status).toBe("ok");
  expect((await request.post("/api/health")).status()).toBe(405);
});
