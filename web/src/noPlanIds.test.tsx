// E8.9 (D122): no plan (D#) or card (E#.#) ids in anything the Tower renders.
// (1) Every string literal, template literal and JSX text node in the SPA source (comments,
//     imports, test files and the generated client excluded) is id-free. Rendered text comes
//     from these literals plus API data; the API side is guarded by tests/test_no_plan_ids_e89.py.
// (2) The Experiments detail renders from its fixture with no ids and a section titled "Spec".
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import ts from "typescript";
import { describe, expect, it } from "vitest";

import { K3_D119_DETAIL } from "./lib/experiments.d119.fixture";
import { verdictBasis, verdictNote, type ExperimentDetail } from "./lib/experiments";
import { coreOverrideWarning } from "./lib/universe";
import { SOURCES } from "./lib/tradeDetail";
import { ExperimentBody } from "./pages/Experiments";

const IDS = /\bD\d{1,3}\b|\bE\d{1,2}\.\d+[a-z]?\b/g;
// Vite inlines every source as a string (no node:fs in the app tsconfig).
const ALL = import.meta.glob(["./**/*.ts", "./**/*.tsx", "!./**/*.test.ts", "!./**/*.test.tsx", "!./**/*.fixture.ts", "!./lib/api.gen.ts", "!./**/*.d.ts"], {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

function ids(text: string): string[] {
  return text.match(IDS) ?? [];
}

/** Text a user can see: string/template literals and JSX text, minus module specifiers. */
function literals(file: string, code: string): { line: number; text: string }[] {
  const sf = ts.createSourceFile(file, code, ts.ScriptTarget.Latest, true, file.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
  const out: { line: number; text: string }[] = [];
  const visit = (n: ts.Node): void => {
    if (ts.isImportDeclaration(n) || ts.isExportDeclaration(n)) return;
    let text: string | null = null;
    if (ts.isStringLiteral(n) || ts.isNoSubstitutionTemplateLiteral(n)) text = n.text;
    else if (ts.isTemplateHead(n) || ts.isTemplateMiddle(n) || ts.isTemplateTail(n)) text = n.text;
    else if (ts.isJsxText(n)) text = n.text;
    if (text !== null) out.push({ line: sf.getLineAndCharacterOfPosition(n.getStart()).line + 1, text });
    ts.forEachChild(n, visit);
  };
  visit(sf);
  return out;
}

describe("no plan/card ids in SPA text (D122)", () => {
  it("the id pattern catches plan and card ids, not units or tickers", () => {
    expect(ids("D32 order budget, E24.2 (D101), E7.5a")).toEqual(["D32", "E24.2", "D101", "E7.5a"]);
    expect(ids("Σ Δ × spot · DTE 45 · SPY · 2D · E-mini")).toEqual([]);
  });

  it("every string literal and JSX text node in web/src is id-free", () => {
    const files = Object.entries(ALL);
    expect(files.length).toBeGreaterThan(30);
    expect(files.some(([f]) => f.endsWith("api.gen.ts") || f.includes(".test."))).toBe(false);
    const bad: string[] = [];
    for (const [f, code] of files) {
      for (const { line, text } of literals(f, code)) {
        if (ids(text).length) bad.push(`${f}:${line}: ${text.trim().slice(0, 120)}`);
      }
    }
    expect(bad).toEqual([]);
  });

  it("lib helpers that used to name a plan id read naturally", () => {
    expect(coreOverrideWarning(Array.from({ length: 40 }, (_, i) => `T${i}`))).toBe(
      "40 names > 30: an older flat list, so the resolver uses config/universe.yaml core instead.",
    );
    expect(SOURCES.find((s) => s.block === "Run manifest")?.tables).toBe("run_manifests");
    const vb = verdictBasis({ analysis: null });
    expect(vb.preAdjustment).toBe(true);
    expect(verdictNote({ basis: "adjusted" } as Parameters<typeof verdictNote>[0], vb)).toBe(
      "verdict on unadjusted — spec pre-dates the SPY adjustment",
    );
  });
});

function render(d: ExperimentDetail): string {
  return renderToStaticMarkup(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <ExperimentBody d={d} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function visibleText(html: string): string {
  return html
    .replace(/<[^>]+>/g, " ")
    .replace(/&[a-z#0-9]+;/gi, " ")
    .replace(/\s+/g, " ");
}

describe("Experiments detail renders without ids (D122)", () => {
  it("the spec section is titled Spec and keeps its hash rows", () => {
    const html = render(K3_D119_DETAIL);
    expect(html).toContain(">Spec<");
    expect(html).not.toContain("Spec and Hashes");
    expect(html).toContain("Spec Hash");
  });

  it("the rendered detail text carries no plan/card ids outside the spec's own prose", () => {
    // The fixture's hypothesis is owner-written spec text (stored data), so it is blanked here.
    const d = { ...K3_D119_DETAIL, spec: { ...K3_D119_DETAIL.spec, hypothesis: "", notes: "" } } as ExperimentDetail;
    expect(ids(visibleText(render(d)))).toEqual([]);
  });
});
