import { expect, test, type Page } from "@playwright/test";

import { expectMinFontSize, expectNoOverflow, expectTouchTargets, PHONE_75, PHONE_75_TAG } from "./mobile";

const OPS_URL = process.env.ARC_E2E_OPS_URL ?? `http://127.0.0.1:${process.env.ARC_E2E_OPS_PORT ?? "4184"}`;

// E8.7d Ops & pipeline on the fixture DB with its ops rows (scripts/tower_fixture_ops.py:
// the dispatcher's dry-run plan for yesterday + today persisted with outcomes and D27
// manifests, anchored at server start) at the TOWER_DESIGN §6 viewports x both themes,
// plus one run detail with the undeclared-write contract mismatch.
const VIEWPORTS = [
  { name: "mobile", width: 390, height: 844 },
  PHONE_75, // E8.8a: the owner's iPhone at 75 % zoom (520x1125); phone-75 project only
  { name: "tablet", width: 768, height: 1024 },
  { name: "desktop", width: 1440, height: 900 },
] as const;
const THEMES = ["dark", "light"] as const;
// E8.8d: the owner's widget order (Alerts / Halts / Runs are collapsible sections).
const CARDS = ["Session Timeline", "Sources", "Health", "LLM Usage", "Context Store", "Auto-Approve", "Config"];
const ORDER = ["Session Timeline", "Sources", "Health", "LLM Usage", "Context Store", "Auto-Approve", "Alerts", "Halts", "Runs", "Config"];

test.use({ baseURL: OPS_URL });

async function open(page: Page, path: string, theme: (typeof THEMES)[number] = "dark") {
  await page.addInitScript((t) => localStorage.setItem("arc.theme", t), theme);
  await page.goto(path);
  await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
}

async function undeclaredRun(page: Page): Promise<string> {
  const res = await page.request.get("/api/ops/runs?job=director&status=ok&size=200&day=today");
  const body = (await res.json()) as { rows: Array<{ run_id: string }> };
  for (const r of body.rows) {
    const d = (await (await page.request.get(`/api/ops/runs/${r.run_id}`)).json()) as {
      step: { contract: { ok: boolean } };
    };
    if (!d.step.contract.ok) return r.run_id;
  }
  throw new Error("fixture has no undeclared-write run");
}

for (const vp of VIEWPORTS) {
  for (const theme of THEMES) {
    test.describe(`ops ${vp.name} ${vp.width}x${vp.height} ${theme}`, { tag: vp.name === PHONE_75.name ? PHONE_75_TAG : [] }, () => {
      test.use({ viewport: { width: vp.width, height: vp.height } });

      test("renders every section of the Ops page", async ({ page }) => {
        await open(page, "/ops", theme);
        await expect(page.getByTestId("ops")).toBeVisible();
        for (const title of CARDS)
          await expect(page.getByRole("heading", { level: 2, name: title, exact: true })).toBeVisible();
        await expect(page.getByRole("heading", { level: 2, name: "Order Budget" })).toHaveCount(0);
        await expect(page.getByTestId("order-budget")).toHaveCount(0);
        const phone = vp.width <= 768;
        const tl = page.getByTestId("session-timeline");
        await expect(tl).toHaveAttribute("data-layout", phone ? "list" : "gantt");
        await expect(tl.getByTestId("band").first()).toBeVisible();
        await expect(page.getByTestId("loop-row")).toBeAttached();
        await expect(page.getByTestId("loop-row").locator('[data-status="no_change"]').first()).toBeAttached();
        await expect(tl.locator('[data-status="failed"]').first()).toBeAttached();
        await expect(page.getByTestId("health-strip").getByTestId("health-chip")).toHaveCount(5);
        await expect(page.getByTestId("health-strip")).toContainText("Tick ok");
        // Alerts, Halts, Runs: closed on a first visit; the header pills still signal trouble
        for (const name of ["Alerts", "Halts", "Runs"])
          await expect(page.getByRole("button", { name: new RegExp(`^${name}`) })).toHaveAttribute("aria-expanded", "false");
        await expect(page.getByTestId("alerts-open")).toContainText("open");
        await expect(page.getByTestId("llm-today")).toContainText("$");
        await expect(page.getByTestId("stacked-bars").locator("svg path").first()).toBeVisible();
        await expect(page.getByTestId("auto-approve")).toContainText("Scorecard gate");
        await expect(page.getByTestId("config-link").getByRole("link")).toHaveAttribute("href", "/ops/config");
        await expectNoOverflow(page);
        if (vp.width <= 520) {
          await expectTouchTargets(page, "[data-testid=ops] [data-testid=session-timeline]");
          await expectTouchTargets(page, "[data-testid=ops] [data-testid=sources]");
          await expectMinFontSize(page, "[data-testid=ops]");
        }
        await page.screenshot({ path: `e2e/screenshots/ops-${vp.name}-${theme}.png`, fullPage: true });
      });

      test("run detail highlights the undeclared write", async ({ page }) => {
        const runId = await undeclaredRun(page);
        await open(page, `/ops/runs/${runId}`, theme);
        await expect(page.getByTestId("run-detail")).toBeVisible();
        await expect(page.getByTestId("contract-mismatch")).toContainText("write proposal");
        await expect(page.getByTestId("contract").locator("tr[data-mismatch]")).toHaveCount(1);
        await expect(page.getByTestId("chain-steps")).toBeVisible();
        await expect(page.getByTestId("persona-calls")).toContainText("Director");
        await expect(page.getByTestId("run-log")).toContainText("context.undeclared_write");
        const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
        expect(overflow).toBeLessThanOrEqual(0);
        await page.screenshot({ path: `e2e/screenshots/ops-run-${vp.name}-${theme}.png`, fullPage: true });
      });
    });
  }
}

test.describe("ops behaviour", () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test("widgets follow the owner's order; no Order Budget", async ({ page }) => {
    await open(page, "/ops");
    await expect(page.getByTestId("health-chip").first()).toBeVisible();
    await expect(page.getByTestId("auto-approve")).toBeVisible();
    await expect(page.getByTestId("source-category").first()).toBeVisible();
    const titles = await page
      .getByTestId("ops")
      .locator(":scope > section")
      .evaluateAll((els) =>
        els.map((el) => (el.querySelector("h2") ?? el.querySelector("header button"))?.textContent?.replace(/[▶▼]/g, "").trim() ?? ""),
      );
    expect(titles.map((t) => ORDER.find((o) => t.startsWith(o)))).toEqual(ORDER);
  });

  test("day control and run filters are URL-synced; a run row opens its detail", async ({ page }) => {
    await open(page, "/ops");
    await page.getByRole("button", { name: "Yesterday", exact: true }).click();
    await expect(page).toHaveURL(/day=yesterday/);
    await expect(page.getByTestId("session-timeline").locator('[data-status="future"]')).toHaveCount(0);
    await page.getByRole("button", { name: /^Runs/ }).click();
    await page.getByLabel("Status").selectOption("failed");
    await expect(page).toHaveURL(/status=failed/);
    await expect(page.getByTestId("run-total")).toHaveText("1 runs");
    await page.getByTestId("runs").getByText("edgar").first().click();
    await expect(page).toHaveURL(/\/ops\/runs\/run-ops-edgar-/);
    await expect(page.getByTestId("run-detail")).toContainText("HTTPError: 503");
  });

  test("a timeline slot opens its run; context links open the entry", async ({ page }) => {
    await open(page, "/ops");
    await page.getByTestId("loop-row").locator('[data-status="done"]').first().click();
    await expect(page).toHaveURL(/\/ops\/runs\/run-ops-director-/);
    await page.getByRole("link", { name: /^candidate:/ }).first().click();
    await expect(page).toHaveURL(/\/ops\/context\//);
    await expect(page.getByTestId("context-entry")).toContainText("candidate");
  });

  test("timeline bands, persona chips and the ⓘ about line come from routines.yaml", async ({ page }) => {
    await open(page, "/ops");
    const tl = page.getByTestId("session-timeline");
    await expect(tl.getByTestId("band").first()).toBeVisible();
    const bands = await tl.getByTestId("band").evaluateAll((els) => els.map((e) => e.getAttribute("data-band")));
    expect(bands[0]).toMatch(/^sources\./);
    expect(bands.indexOf("scout")).toBeLessThan(bands.indexOf("trading_loop"));
    expect(bands.indexOf("trading_loop")).toBeLessThan(bands.indexOf("position_management"));
    const loop = page.getByTestId("loop-row");
    await expect(loop).toContainText("Director → Quant → Risk → Propose → Execute");
    await expect(loop.getByTestId("persona-chip")).toHaveText("Director");
    await expect(tl.locator('[data-job="positions.evaluate"]')).toContainText("Investor exits");
    await loop.getByTestId("job-info").click();
    await expect(page.getByRole("dialog").or(page.locator("[role=tooltip]")).first()).toContainText("LLM yes");
  });

  test("health chips, auto-approve key/values and grouped alerts", async ({ page }) => {
    await open(page, "/ops");
    const failed = page.locator('[data-testid=health-chip][data-status="failed"]');
    await expect(failed).toHaveCount(1);
    await expect(failed.getByTestId("health-message")).toBeVisible(); // non-ok chips expand inline
    await expect(page.getByTestId("auto-approve")).toContainText("Paper");
    await expect(page.getByTestId("auto-approve")).toContainText("Last flip");
    await page.getByRole("button", { name: /^Alerts/ }).click();
    await expect(page.getByTestId("alerts")).toContainText(/missed_window ×\d+/);
    await page.getByRole("button", { name: /^Halts/ }).click();
    await expect(page.getByTestId("halts")).toContainText("daily_loss");
  });

  test("sources are grouped by D47 category with YouTube brief status", async ({ page }) => {
    await open(page, "/ops");
    const src = page.getByTestId("sources");
    const cats = src.getByTestId("source-category");
    await expect(cats.first()).toBeVisible();
    expect(await cats.count()).toBeGreaterThanOrEqual(4);
    await expect(cats.first()).toContainText("max_age");
    await expect(src).toContainText("backoff");
    await expect(src).toContainText("skipped (stale)");
    await expect(src.getByTestId("brief-status").filter({ hasText: "brief ok" }).first()).toBeVisible();
    await expect(src.getByTestId("brief-status").filter({ hasText: "pending: no captions yet" }).first()).toBeVisible();
    await expect(src.getByTestId("brief-status").filter({ hasText: /no video in/ }).first()).toBeVisible();
  });

  test("the effective config lives on /ops/config", async ({ page }) => {
    await open(page, "/ops");
    await page.getByTestId("config-link").getByRole("link").click();
    await expect(page).toHaveURL(/\/ops\/config$/);
    await expect(page.getByTestId("config")).toContainText("account_profile");
    await expect(page.getByTestId("config-changes")).toContainText("revert");
  });
});

test.describe("ops phone-75", { tag: PHONE_75_TAG }, () => {
  test("timeline is a grouped list; bands and categories collapse; sections start closed", async ({ page }) => {
    await open(page, "/ops");
    const tl = page.getByTestId("session-timeline");
    await expect(tl).toHaveAttribute("data-layout", "list");
    await expect(page.locator(".min-w-\\[720px\\]")).toHaveCount(0); // no min-width Gantt
    // a band with a failed / missed slot starts open; a clean one starts closed
    const bands = tl.getByTestId("band");
    await expect(bands.first()).toBeVisible();
    const states = await bands.evaluateAll((els) => els.map((e) => e.getAttribute("data-open")));
    expect(states).toContain("true");
    // expand a closed band, then tap a job row for its slot details
    const [closedKey] = await tl.locator('[data-testid=band][data-open="false"]').evaluateAll((els) => els.map((e) => e.getAttribute("data-band")));
    if (closedKey) {
      const closed = tl.locator(`[data-testid=band][data-band="${closedKey}"]`);
      await closed.getByTestId("band-header").click();
      await expect(closed).toHaveAttribute("data-open", "true");
    }
    const row = tl.getByTestId("loop-row");
    const loopBand = tl.locator("[data-testid=band][data-band=trading_loop]");
    if ((await loopBand.getAttribute("data-open")) === "false") await loopBand.getByTestId("band-header").click();
    await expect(row.getByTestId("row-summary")).toContainText(/\d+\/\d+ done/);
    await row.locator("button[aria-controls]").first().click();
    await expect(row.getByTestId("slot-details")).toBeVisible();
    // Sources: categories show the header only, unless they have a problem
    const cats = page.getByTestId("source-category");
    await expect(cats.first()).toBeVisible();
    const catState = await cats.evaluateAll((els) => els.map((e) => [e.getAttribute("data-status"), e.getAttribute("data-open")]));
    for (const [status, isOpen] of catState) expect(isOpen).toBe(["pending", "backoff", "late", "failed"].includes(status!) ? "true" : "false");
    const [shutKey] = await page.locator('[data-testid=source-category][data-open="false"]').evaluateAll((els) => els.map((e) => e.getAttribute("data-category")));
    if (shutKey) {
      const shut = page.locator(`[data-testid=source-category][data-category="${shutKey}"]`);
      await shut.getByRole("button").first().click();
      await expect(shut).toHaveAttribute("data-open", "true");
      await shut.getByRole("button").first().click();
      await expect(shut).toHaveAttribute("data-open", "false");
    }
    for (const name of ["Alerts", "Halts", "Runs"])
      await expect(page.getByRole("button", { name: new RegExp(`^${name}`) })).toHaveAttribute("aria-expanded", "false");
    await expect(page.getByText("Order Budget")).toHaveCount(0);
    await expectNoOverflow(page);
  });
});
