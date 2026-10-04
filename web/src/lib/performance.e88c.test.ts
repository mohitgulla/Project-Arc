// E8.8c (D48) guard: the Performance page has one range selector and no period comparison or
// test-leg toggle. Scans every source under web/src except tests and the generated API client
// (`api.gen.ts` mirrors the server, which still accepts `compare` for back-compat until a
// cleanup PR removes it).
import { describe, expect, it } from "vitest";

const SOURCES = {
  ...(import.meta.glob("../**/*.{ts,tsx,css}", { query: "?raw", import: "default", eager: true }) as Record<
    string,
    string
  >),
};

const BANNED = [/compare/i, /previous period/i, /include paper test legs/i, /include_tests/];

describe("Performance has no compare / test-leg controls (E8.8c)", () => {
  it("scans the sources (not silently empty)", () => {
    expect(Object.keys(SOURCES).length).toBeGreaterThan(40);
    expect(Object.keys(SOURCES)).toContain("../pages/Performance.tsx");
  });

  it("no banned string outside tests and the generated client", () => {
    const hits = Object.entries(SOURCES)
      .filter(([file]) => !/\.test\.tsx?$/.test(file) && !file.endsWith("/api.gen.ts"))
      .flatMap(([file, src]) => BANNED.filter((re) => re.test(src)).map((re) => `${file}: ${re}`));
    expect(hits).toEqual([]);
  });
});
