import { expect, type Page } from "@playwright/test";

/**
 * Shared E8.8 mobile helpers (TOWER_DESIGN §10, D48). Later E8.8 cards call these per page.
 *
 * The `phone-75` Playwright project is the owner's iPhone in Safari at 75 % page zoom: about
 * 520 CSS px wide. Tag a describe block `{ tag: PHONE_75_TAG }` to run it there (the device is
 * applied by the project); the default project skips the tag.
 */
export const PHONE_75_TAG = "@phone-75";

export const PHONE_UA =
  "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1";

export const PHONE_75 = { name: "phone-75", width: 520, height: 1125, layout: "mobile" } as const;

export const PHONE_75_DEVICE = {
  viewport: { width: PHONE_75.width, height: PHONE_75.height },
  isMobile: true,
  hasTouch: true,
  userAgent: PHONE_UA,
  deviceScaleFactor: 3,
} as const;

/** Options for `test.use(...)` for one viewport entry: the phone entry gets the full touch device. */
export function viewportUse(vp: { name: string; width: number; height: number }) {
  return vp.name === PHONE_75.name ? { ...PHONE_75_DEVICE } : { viewport: { width: vp.width, height: vp.height } };
}

/** Viewports where the mobile acceptance (overflow, touch targets, min text) applies. */
export function isPhoneWidth(page: Page): boolean {
  const w = page.viewportSize()?.width ?? 0;
  return w <= 520;
}

/**
 * No horizontal overflow: the page is no wider than the viewport, and no `.arc-card` is wider
 * than its box, except inside declared scroll containers (`data-scroll-x`, `.arc-scroll-x`).
 */
export async function expectNoOverflow(page: Page) {
  const report = await page.evaluate(() => {
    const pageOver = document.documentElement.scrollWidth - window.innerWidth;
    const cards: string[] = [];
    for (const card of Array.from(document.querySelectorAll<HTMLElement>(".arc-card"))) {
      if (card.closest("[data-scroll-x], .arc-scroll-x")) continue;
      if (card.scrollWidth > card.clientWidth + 1) {
        const title = card.querySelector("h2")?.textContent?.trim() ?? card.className.slice(0, 40);
        cards.push(`${title}: ${card.scrollWidth} > ${card.clientWidth}`);
      }
    }
    return { pageOver, cards };
  });
  expect(report.pageOver, "document scrollWidth exceeds innerWidth").toBeLessThanOrEqual(0);
  expect(report.cards, "cards wider than their box").toEqual([]);
}

/**
 * Every visible interactive element inside `scope` offers a hit area ≥ `min` px on both axes.
 * The `.arc-hit` pseudo-element counts (the visible glyph may be smaller than its target).
 */
export async function expectTouchTargets(page: Page, scope = "body", min = 44) {
  const small = await page.evaluate(
    ({ scope, min }) => {
      const root = document.querySelector(scope);
      if (!root) return [`no ${scope}`];
      const sel = "a[href], button, [role=tab], select, input:not([type=hidden]), summary";
      const out: string[] = [];
      for (const el of Array.from(root.querySelectorAll<HTMLElement>(sel))) {
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) continue;
        if (getComputedStyle(el).visibility === "hidden") continue;
        // Inline text links inside a sentence or table cell are exempt (WCAG 2.5.8 inline).
        if (el.tagName === "A" && el.closest("p, td, li") && !el.closest("nav")) continue;
        let w = r.width;
        let h = r.height;
        const after = getComputedStyle(el, "::after");
        if (after.content !== "none" && after.position === "absolute") {
          w = Math.max(w, parseFloat(after.width) || 0);
          h = Math.max(h, parseFloat(after.height) || 0);
        }
        if (w < min - 0.5 || h < min - 0.5) {
          const name = el.getAttribute("aria-label") ?? el.textContent?.trim().slice(0, 30) ?? el.tagName;
          out.push(`${el.tagName.toLowerCase()} "${name}" ${Math.round(w)}x${Math.round(h)}`);
        }
      }
      return out;
    },
    { scope, min },
  );
  expect(small, `touch targets under ${min}px in ${scope}`).toEqual([]);
}

/** No rendered text below --fs-micro (11 px); the decorative superscript `$` glyph is exempt. */
export async function expectMinFontSize(page: Page, scope = "body", min = 11) {
  const tiny = await page.evaluate(
    ({ scope, min }) => {
      const root = document.querySelector(scope);
      if (!root) return [`no ${scope}`];
      const out = new Set<string>();
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      for (let n = walker.nextNode(); n; n = walker.nextNode()) {
        const text = n.textContent?.trim();
        const el = n.parentElement;
        if (!text || !el || el.closest("svg")) continue;
        // The §2 superscript currency glyph (0.62em `$` before a number) is decorative.
        if (el.closest(".arc-money-glyph")) continue;
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) continue;
        const px = parseFloat(getComputedStyle(el).fontSize);
        if (px < min - 0.01) out.add(`"${text.slice(0, 24)}" ${px}px`);
      }
      return [...out];
    },
    { scope, min },
  );
  expect(tiny, `text under ${min}px in ${scope}`).toEqual([]);
}
