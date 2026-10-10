import { expect, test, type Page } from "@playwright/test";

import { expectMinFontSize, expectNoOverflow, PHONE_75, PHONE_75_TAG, viewportUse } from "./mobile";

const OPS_URL = process.env.ARC_E2E_OPS_URL ?? `http://127.0.0.1:${process.env.ARC_E2E_OPS_PORT ?? "4184"}`;

// E13.14 (D56): Positions exit path (scripts/tower_fixture_exits.py: shadow chain on SPY /
// QQQ / NVDA), the Idea Funnel card and the Universe tail cuts, on the --ops fixture.
// Persona labels come from /api/meta, never from web/src.
const VIEWPORTS = [PHONE_75, { name: "desktop", width: 1440, height: 900 }] as const;

test.use({ baseURL: OPS_URL });

async function open(page: Page, path: string) {
  await page.addInitScript(() => localStorage.setItem("arc.theme", "dark"));
  await page.goto(path);
}

for (const vp of VIEWPORTS) {
  test.describe(`d56 ${vp.name} ${vp.width}x${vp.height}`, { tag: vp.name === PHONE_75.name ? PHONE_75_TAG : [] }, () => {
    test.use(viewportUse(vp));

    test("positions show the exit path strip, columns and details", async ({ page }) => {
      await open(page, "/positions");
      const strip = page.getByTestId("exit-path-strip");
      // 3 cases: the exits fixture's SPY + QQQ, plus the --ops fixture's generic exit_case row.
      await expect(strip).toHaveText("Exit path · Research · 1 mandatory pending · 3 cases today · 1 close proposed · 1 hold");
      if (vp.width > 768) {
        await expect(page.getByTestId("exit-watch").first()).toBeVisible();
        await expect(page.getByTestId("exit-verdict")).toHaveCount(2);
        await expect(page.getByTestId("mandatory-signal")).toHaveText("stop");
      }
      const details = page.getByTestId("exit-details");
      await details.locator("summary").click();
      await expect(page.getByTestId("exit-detail-SPY")).toContainText("Review · Weakened");
      await expect(page.getByTestId("exit-detail-SPY")).toContainText("story:st-1 breadth fading into CPI");
      await expect(page.getByTestId("exit-detail-SPY")).toContainText("Risk Close");
      await expect(page.getByTestId("exit-detail-NVDA")).toContainText("stop");
      await expectNoOverflow(page);
      if (vp.width <= 520) await expectMinFontSize(page, "[data-testid=exit-details]");
      await page.screenshot({ path: `e2e/screenshots/d56-positions-${vp.name}.png`, fullPage: true });
    });

    test("performance shows the idea funnel split by feed", async ({ page }) => {
      await open(page, "/performance?range=1W");
      const f = page.getByTestId("idea-funnel");
      await expect(f.locator("li")).toHaveCount(10);
      await expect(f.locator('li[data-stage="docs_fresh"]')).toContainText("Docs fresh");
      await expect(f.locator('li[data-stage="docs_fresh"]')).toContainText("⚡ Scalp");
      await expect(f.locator('li[data-stage="docs_fresh"]')).toContainText("🔭 Scout");
      await expect(page.getByTestId("funnel-discovery-fill")).toContainText("Discovery fill (Scout)");
      await expectNoOverflow(page);
      await f.scrollIntoViewIfNeeded();
      await page.screenshot({ path: `e2e/screenshots/d56-funnel-${vp.name}.png`, fullPage: true });
    });

    test("universe shows the tail cuts", async ({ page }) => {
      await open(page, "/ops/universe");
      // E14.10 (D67): tail cuts live in the closed `Dropped & Reference` disclosure
      await page.getByTestId("uni-dropped-ref").locator("summary").click();
      await expect(page.getByTestId("uni-tail-cuts")).toBeVisible();
      // the --ops fixture's resolve cuts one name at the active cap (BBAI, discovery)
      await expect(page.getByTestId("uni-tail-cut")).toHaveCount(1);
      await expect(page.getByTestId("uni-tail-cut").first()).toContainText("Discovery");
      await expectNoOverflow(page);
      await page.screenshot({ path: `e2e/screenshots/d56-universe-${vp.name}.png`, fullPage: true });
    });

    test("persona chips come from /api/meta", async ({ page }) => {
      await open(page, "/ops");
      // Timeline bands collapse on a phone: the chip is in the DOM, not necessarily visible.
      await expect(page.getByTestId("persona-chip").filter({ hasText: "⚡ Scalp" }).first()).toBeAttached();
      const text = await page.locator("body").innerText();
      expect(text).not.toMatch(/\b(Director|Investor|Auditor|Sweep)\b/);
    });
  });
}
