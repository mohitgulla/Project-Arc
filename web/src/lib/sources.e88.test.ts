// E8.8a (D48) guards over the page sources: widget titles are Title Case, and no card uses
// the removed footer `asOf` slot (freshness lives on the header line, TOWER_DESIGN §10).
import { describe, expect, it } from "vitest";

import { isTitleCase, titleCase } from "./format";

// Vite inlines every page/component source as a string (no node:fs in the app tsconfig).
const PAGES = import.meta.glob("../pages/**/*.tsx", { query: "?raw", import: "default", eager: true }) as Record<string, string>;
const COMPONENTS = import.meta.glob("../components/**/*.tsx", { query: "?raw", import: "default", eager: true }) as Record<string, string>;
const LIBS = import.meta.glob("./*.ts", { query: "?raw", import: "default", eager: true }) as Record<string, string>;
const ALL = { ...PAGES, ...COMPONENTS, ...LIBS };

// `<Card title="…"`, `<StatCard … title="…"`, `<Section title="…"`, `<SectionBlock title="…"`:
// a string-literal title inside the component's opening tag (no other tag in between).
const TAG_TITLE = /<(Card|StatCard|Section|SectionBlock)\b[^<>]*?\btitle="([^"]*)"/g;
// Run-manifest group titles in lib/ops.ts render as Card titles on the run detail.
const GROUP_TITLE = /^\s+title: "([^"]*)",$/gm;

function widgetTitles(): Array<{ file: string; title: string }> {
  const out: Array<{ file: string; title: string }> = [];
  for (const [file, src] of Object.entries(PAGES))
    for (const m of src.matchAll(TAG_TITLE)) out.push({ file, title: m[2]! });
  for (const m of (LIBS["./ops.ts"] ?? "").matchAll(GROUP_TITLE)) out.push({ file: "lib/ops.ts", title: m[1]! });
  return out;
}

describe("widget titles are Title Case (D48)", () => {
  it("finds the page's title literals (the scan is not silently empty)", () => {
    expect(widgetTitles().length).toBeGreaterThan(60);
  });

  it("every Card / StatCard / Section title literal is Title Case", () => {
    const bad = widgetTitles()
      .filter(({ title }) => !isTitleCase(title))
      .map(({ file, title }) => `${file}: "${title}" -> "${titleCase(title)}"`);
    expect(bad).toEqual([]);
  });
});

describe("one freshness slot (TOWER_DESIGN §10)", () => {
  it("no asOf= prop is left anywhere in web/src", () => {
    const hits = Object.entries(ALL)
      .filter(([file]) => !file.endsWith(".test.ts"))
      .flatMap(([file, src]) => (src.match(/\basOf=/g) ? [file] : []));
    expect(hits).toEqual([]);
  });

  it("the separate Greeks `stale` pill is gone (the header badge carries it)", () => {
    expect(PAGES["../pages/Overview.tsx"]).not.toContain("greeks-stale");
  });
});
