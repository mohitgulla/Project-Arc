import { expect, test, type Page, type Request } from "@playwright/test";

import { PHONE_75, PHONE_75_TAG, expectNoOverflow, expectTouchTargets } from "./mobile";

// E15.7 (D69, D92): the global arm switcher on the --arms store (the --ops fixture plus a
// running K=3 experiment XP-12 with arm stores t1..t3; scripts/tower_fixture_arms.py).
// Arc Investor is the default on every load (no localStorage), `?arm=` deep-links an arm,
// the banner shows on every page while an arm is selected, and Back to Arc Investor works.
const ARMS_URL = process.env.ARC_E2E_ARMS_URL ?? `http://127.0.0.1:${process.env.ARC_E2E_ARMS_PORT ?? "4185"}`;
const ARM = "XP-12:t1";
const PAGES = [
  { path: "/", heading: "Overview" },
  { path: "/trades", heading: "Trades" },
  { path: "/positions", heading: "Positions" },
  { path: "/performance", heading: "Performance" },
  { path: "/experiments", heading: "Experiments" },
  { path: "/ops", heading: "Ops" },
] as const;

test.use({ baseURL: ARMS_URL });

/** Every `/api/*` request the page makes, to check which account it reads. */
function apiLog(page: Page): Request[] {
  const seen: Request[] = [];
  page.on("request", (r) => {
    if (new URL(r.url()).pathname.startsWith("/api/")) seen.push(r);
  });
  return seen;
}

const armOf = (r: Request) => new URL(r.url()).searchParams.get("arm");

async function open(page: Page, path: string, theme: "light" | "dark" = "dark") {
  await page.addInitScript((t) => localStorage.setItem("arc.theme", t), theme);
  await page.goto(path);
  await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
}

test.describe("arm switcher", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("defaults to Arc Investor on load, with no arm on any API call", async ({ page }) => {
    const calls = apiLog(page);
    await open(page, "/");
    await expect(page.getByTestId("account-slot")).toHaveText("Paper Trade · Arc Investor");
    await expect(page.getByTestId("arm-banner")).toHaveCount(0);
    await expect(page.getByTestId("overview")).toBeVisible();
    // Scoped calls carry no ?arm (useControlMeta never does either).
    expect(calls.length).toBeGreaterThan(0);
    expect(calls.filter((r) => armOf(r) !== null).map((r) => r.url())).toEqual([]);
    // Other pages show the header pill instead of the strip's slot.
    await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Trades" }).click();
    await expect(page.getByTestId("arm-switcher")).toHaveText("Arc Investor");
    await expect(page.getByTestId("arm-switcher")).toHaveAttribute("data-arm", "control");
  });

  test("the menu lists Arc Investor and the running arms; choosing one scopes every call", async ({ page }) => {
    const calls = apiLog(page);
    await open(page, "/");
    await page.getByTestId("account-switcher").click();
    const menu = page.getByTestId("arm-menu");
    await expect(menu).toBeVisible();
    const items = menu.getByRole("menuitemradio");
    await expect(items).toHaveCount(4);
    await expect(items.nth(0)).toContainText("Arc Investor");
    await expect(items.nth(0)).toHaveAttribute("aria-checked", "true");
    for (const [i, t] of ["T1", "T2", "T3"].entries()) await expect(items.nth(i + 1)).toContainText(`XP-12 · ${t}`);
    await expect(menu).not.toContainText(/control/i);
    calls.length = 0;
    await items.nth(2).click();
    await expect(page).toHaveURL(/[?&]arm=XP-12%3At2/);
    await expect(page.getByTestId("account-slot")).toHaveText("Paper Trade · XP-12 · T2");
    await expect(page.getByTestId("arm-banner")).toContainText("Viewing XP-12 · T2");
    // The arm's own virtual account: $25k-ish, not Arc Investor's $100k book.
    await expect(page.getByTestId("overview")).toContainText("24,8");
    const scoped = calls.filter((r) => !/\/api\/meta$/.test(new URL(r.url()).pathname) || armOf(r) !== null);
    expect(scoped.length).toBeGreaterThan(0);
    expect(scoped.filter((r) => armOf(r) !== "XP-12:t2").map((r) => r.url())).toEqual([]);
  });

  test("a deep link with ?arm= restores the arm and keeps it across in-app navigation", async ({ page }) => {
    const calls = apiLog(page);
    await open(page, `/?arm=${encodeURIComponent(ARM)}`);
    await expect(page.getByTestId("account-slot")).toHaveText("Paper Trade · XP-12 · T1");
    await expect(page.getByTestId("arm-banner")).toHaveText(
      "Viewing XP-12 · T1 — virtual account, shared inputs from Arc InvestorBack to Arc Investor",
    );
    const overview = calls.filter((r) => new URL(r.url()).pathname === "/api/overview");
    expect(overview.length).toBeGreaterThan(0);
    expect(overview.every((r) => armOf(r) === ARM)).toBe(true);
    // Sidebar links carry no ?arm; the provider puts it back.
    await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Positions" }).click();
    await expect(page).toHaveURL(/\/positions\?(.*&)?arm=XP-12%3At1/);
    await expect(page.getByTestId("arm-switcher")).toHaveText("XP-12 · T1");
    await expect(page.getByTestId("arm-banner")).toBeVisible();
  });

  test("the banner shows on every page while an arm is selected", async ({ page }) => {
    for (const p of PAGES) {
      await open(page, `${p.path}?arm=${encodeURIComponent(ARM)}`);
      await expect(page.getByRole("heading", { level: 1, name: p.heading })).toBeVisible();
      await expect(page.getByTestId("arm-banner"), p.path).toContainText("Viewing XP-12 · T1");
      await expect(page.getByTestId("arm-back"), p.path).toHaveText("Back to Arc Investor");
    }
  });

  test("Back to Arc Investor clears the arm, and a fresh load never remembers it", async ({ page }) => {
    await open(page, `/trades?arm=${encodeURIComponent(ARM)}`);
    await expect(page.getByTestId("arm-banner")).toBeVisible();
    const calls = apiLog(page);
    await page.getByTestId("arm-back").click();
    await expect(page).toHaveURL(/\/trades$/);
    await expect(page.getByTestId("arm-banner")).toHaveCount(0);
    await expect(page.getByTestId("arm-switcher")).toHaveText("Arc Investor");
    await expect.poll(() => calls.some((r) => new URL(r.url()).pathname === "/api/trades" && armOf(r) === null)).toBe(true);
    // Choose an arm, then load a URL without ?arm: Arc Investor (owner: no persistence).
    await open(page, `/?arm=${encodeURIComponent(ARM)}`);
    await expect(page.getByTestId("arm-banner")).toBeVisible();
    await page.goto("/");
    await expect(page.getByTestId("account-slot")).toHaveText("Paper Trade · Arc Investor");
    await expect(page.getByTestId("arm-banner")).toHaveCount(0);
    expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain("XP-12");
  });

  test("an unknown arm says so and offers the way back", async ({ page }) => {
    await open(page, "/?arm=XP-99:t1");
    await expect(page.getByTestId("arm-banner")).toContainText("XP-99 · T1 is not a running experiment arm");
    await page.getByTestId("arm-back").click();
    await expect(page.getByTestId("account-slot")).toHaveText("Paper Trade · Arc Investor");
  });

  test("Escape closes the menu and keyboard picks an arm", async ({ page }) => {
    await open(page, "/ops");
    await page.getByTestId("arm-switcher").click();
    await expect(page.getByTestId("arm-menu")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByTestId("arm-menu")).toHaveCount(0);
    await page.getByTestId("arm-switcher").press("Enter");
    await expect(page.getByTestId("arm-menu").getByRole("menuitemradio")).toHaveCount(4);
    // Opening focuses the checked item (Arc Investor); arrows move, Enter picks.
    await expect(page.getByTestId("arm-menu").getByRole("menuitemradio").first()).toBeFocused();
    await page.keyboard.press("ArrowDown");
    await expect(page.getByTestId("arm-menu").getByRole("menuitemradio").nth(1)).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/arm=XP-12%3At1/);
  });
});

// Screenshots for the PR: Overview in Arc Investor and in an arm, phone and desktop, both
// themes; Experiments with the K=3 fixture.
const SHOTS = [
  { name: "desktop", width: 1440, height: 900, tag: [] as string[] },
  { ...PHONE_75, tag: [PHONE_75_TAG] },
] as const;
for (const vp of SHOTS) {
  for (const theme of ["dark", "light"] as const) {
    test.describe(`arm switcher ${vp.name} ${theme}`, { tag: [...vp.tag] }, () => {
      if (vp.name === "desktop") test.use({ viewport: { width: vp.width, height: vp.height } });

      test("overview in Arc Investor and in an arm", async ({ page }) => {
        await open(page, "/", theme);
        await expect(page.getByTestId("overview")).toBeVisible();
        await expectNoOverflow(page);
        await page.screenshot({ path: `e2e/screenshots/arms-overview-control-${vp.name}-${theme}.png` });
        await page.getByTestId("account-switcher").click();
        await expect(page.getByTestId("arm-menu")).toBeVisible();
        if (vp.name !== "desktop") await expectTouchTargets(page, "[data-testid=arm-menu]");
        await page.screenshot({ path: `e2e/screenshots/arms-menu-${vp.name}-${theme}.png` });
        await page.getByTestId("arm-menu").getByRole("menuitemradio").nth(1).click();
        await expect(page.getByTestId("arm-banner")).toBeVisible();
        await expect(page.getByTestId("account-slot")).toHaveText("Paper Trade · XP-12 · T1");
        await expectNoOverflow(page);
        if (vp.name !== "desktop") await expectTouchTargets(page, "[data-testid=arm-banner]");
        await page.screenshot({ path: `e2e/screenshots/arms-overview-arm-${vp.name}-${theme}.png` });
      });

      test("experiments with the K=3 experiment, in an arm", async ({ page }) => {
        await open(page, `/experiments?arm=${encodeURIComponent(ARM)}`, theme);
        await expect(page.getByText("XP-12").first()).toBeVisible();
        await expect(page.getByTestId("arm-banner")).toBeVisible();
        await expectNoOverflow(page);
        await page.screenshot({ path: `e2e/screenshots/arms-experiments-${vp.name}-${theme}.png`, fullPage: true });
      });
    });
  }
}
