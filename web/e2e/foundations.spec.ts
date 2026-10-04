import { expect, test, type Page } from "@playwright/test";

import { expectMinFontSize, expectNoOverflow, expectTouchTargets, PHONE_75, PHONE_75_TAG } from "./mobile";

// E8.8a (D48) foundations on the owner's phone (iPhone Safari at 75 % zoom, ~520 CSS px) and on
// desktop: the §10 header freshness badge, InfoTip, SegmentedControl, CappedList and Section
// persistence, exercised through their /kitchen-sink stories (empty scratch DB, no fixture).
// /kitchen-sink renders every story twice (dark + light panels), so each check is scoped to
// the panel for the theme under test. Blocks tagged `@phone-75` run only in the `phone-75`
// project (playwright.config).
const THEMES = ["dark", "light"] as const;
type Theme = (typeof THEMES)[number];

async function open(page: Page, theme: Theme) {
  await page.addInitScript((t) => localStorage.setItem("arc.theme", t), theme);
  await page.goto("/kitchen-sink");
  await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
  const panel = page.getByTestId(`kitchen-${theme}`);
  await expect(panel.getByTestId("freshness-stories")).toBeVisible();
  return panel;
}

for (const theme of THEMES) {
  test.describe(`foundations ${PHONE_75.name} ${PHONE_75.width}x${PHONE_75.height} ${theme}`, { tag: PHONE_75_TAG }, () => {
    test("stories fit 520 px with touch targets and readable text", async ({ page }) => {
      await open(page, theme);
      expect(page.viewportSize()?.width).toBe(PHONE_75.width);
      await expect(page.locator("[data-layout]")).toHaveAttribute("data-layout", "mobile");
      await expectNoOverflow(page);
      const p = `[data-testid=kitchen-${theme}]`;
      await expectTouchTargets(page, `${p} [data-testid=freshness-stories]`);
      await expectTouchTargets(page, `${p} [data-story=InfoTip]`);
      await expectTouchTargets(page, `${p} [data-story=SegmentedControl]`);
      await expectTouchTargets(page, `${p} [data-story=CappedList]`);
      await expectMinFontSize(page, "main");
      await page.screenshot({ path: `e2e/screenshots/foundations-phone-75-${theme}.png`, fullPage: true });
    });

    test("tap reveals the freshness line and InfoTip, clamped to the viewport", async ({ page }) => {
      const panel = await open(page, theme);
      await panel.getByTestId("freshness-stories").locator("[data-freshness=fresh]").tap();
      const tip = page.getByRole("tooltip");
      await expect(tip).toContainText(/as of \w{3} \d\d-\d\d \d\d:\d\d ET · monitor mark · stale after 15m/);
      const box = (await tip.boundingBox())!;
      expect(box.x).toBeGreaterThanOrEqual(0);
      expect(box.x + box.width).toBeLessThanOrEqual(PHONE_75.width);
      // An outside tap closes it.
      await page.locator("main p").first().tap();
      await expect(tip).toHaveCount(0);

      await panel.getByTestId("infotip-story").tap();
      await expect(page.getByRole("tooltip")).toContainText("EV − spread − slippage − fees");
      const ib = (await page.getByRole("tooltip").boundingBox())!;
      expect(ib.x).toBeGreaterThanOrEqual(0);
      expect(ib.x + ib.width).toBeLessThanOrEqual(PHONE_75.width);
      await expectNoOverflow(page);
    });
  });
}

test.describe("foundations desktop", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("freshness badge states: fresh, stale in --warn, no data", async ({ page }) => {
    const panel = await open(page, "dark");
    const stories = panel.getByTestId("freshness-stories");
    await expect(stories.locator("[data-freshness]")).toHaveCount(3);
    await expect(stories.locator("[data-freshness=fresh]")).toContainText("ago");
    await expect(stories.locator("[data-freshness=stale]")).toHaveText(/^stale · \d+[mhd] ago$/);
    await expect(stories.locator("[data-freshness=none]")).toHaveText("no data");
    // The badge sits on the header line beside the title, not in a footer.
    const header = stories.locator("header").filter({ hasText: "Greeks vs Caps" });
    await expect(header.locator("[data-freshness=stale]")).toBeVisible();
    // Hover reveals the full line on hover devices; Esc closes it.
    await stories.locator("[data-freshness=stale]").hover();
    await expect(page.getByRole("tooltip")).toContainText("stale after 15m");
    await page.keyboard.press("Escape");
    await expect(page.getByRole("tooltip")).toHaveCount(0);
  });

  test("InfoTip opens on keyboard focus and closes on Escape", async ({ page }) => {
    const panel = await open(page, "light");
    const tipBtn = panel.getByTestId("infotip-story");
    await tipBtn.focus();
    await page.keyboard.press("Enter");
    await expect(tipBtn).toHaveAttribute("aria-expanded", "true");
    await expect(page.getByRole("tooltip")).toContainText("managed exit policy");
    await page.keyboard.press("Escape");
    await expect(page.getByRole("tooltip")).toHaveCount(0);
    await expect(tipBtn).toBeFocused();
  });

  test("CappedList shows 8 then all; SegmentedControl switches", async ({ page }) => {
    const panel = await open(page, "dark");
    const list = panel.getByTestId("capped-story");
    await expect(list.locator(":scope > li")).toHaveCount(8);
    await panel.getByTestId("capped-story-more").click();
    await expect(list.locator(":scope > li")).toHaveCount(12);
    await expect(panel.getByTestId("capped-story-more")).toHaveText("Show less");

    const seg = panel.locator("[data-story=SegmentedControl]");
    await expect(seg.getByRole("tab", { name: "Structure" })).toHaveAttribute("aria-selected", "true");
    await seg.getByRole("tab", { name: "Ticker" }).click();
    await expect(seg.getByRole("tab", { name: "Ticker" })).toHaveAttribute("aria-selected", "true");
    await expect(seg.getByRole("tab", { name: "Structure" })).toHaveAttribute("aria-selected", "false");
  });

  test("Section open/closed state survives a reload", async ({ page }) => {
    const panel = await open(page, "dark");
    const story = panel.locator("[data-story^=Section]");
    const collapsed = () => story.getByRole("button", { name: /Collapsed Section/ });
    const proposals = () => story.getByRole("button", { name: /Proposals/ });
    await expect(collapsed()).toHaveAttribute("aria-expanded", "false");
    await collapsed().click();
    await expect(collapsed()).toHaveAttribute("aria-expanded", "true");
    await proposals().click();
    await expect(proposals()).toHaveAttribute("aria-expanded", "false");
    await page.reload();
    await expect(collapsed()).toHaveAttribute("aria-expanded", "true");
    await expect(proposals()).toHaveAttribute("aria-expanded", "false");
    expect(await page.evaluate(() => localStorage.getItem("arc.section:kitchen-sink:collapsed-section"))).toBe("1");
    expect(await page.evaluate(() => localStorage.getItem("arc.section:kitchen-sink:proposals"))).toBe("0");
  });
});
