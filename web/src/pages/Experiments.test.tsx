// E25.8 (D120): the Experiments page renders the SPY-adjusted headline and the per-pair panel,
// and a report written before D119 renders exactly as before (no panel, no chips, same hero).
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import { K3_D119_DETAIL } from "../lib/experiments.d119.fixture";
import type { ExperimentDetail, ExperimentReport } from "../lib/experiments";
import { ExperimentBody } from "./Experiments";

function render(d: ExperimentDetail): string {
  return renderToStaticMarkup(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <ExperimentBody d={d} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function text(html: string, testid: string): string[] {
  const out: string[] = [];
  const re = new RegExp(`data-testid="${testid}"[^>]*>(.*?)</(?:p|span|div|li)>`, "g");
  for (const m of html.matchAll(re)) out.push((m[1] ?? "").replace(/<[^>]+>/g, ""));
  return out;
}

/** The fixture with every D119 field stripped: what a pre-D119 stored report looks like. */
function preD119(d: ExperimentDetail): ExperimentDetail {
  const r = d.report as ExperimentReport;
  const strip = <T extends object>(p: T): T => {
    const q = { ...p } as Record<string, unknown>;
    for (const k of ["adjustment", "hac", "sigma_raw", "sigma_adj", "sigma_lr", "primary_unadjusted"]) delete q[k];
    return q as T;
  };
  const primary = { ...strip(r.primary), mean: r.primary.primary_unadjusted?.mean ?? null, ci: r.primary.primary_unadjusted?.ci ?? null, p_value: r.primary.primary_unadjusted?.p_value ?? null };
  const pairwise = (r.pairwise ?? []).map((p) => ({ ...strip(p), mean: p.primary_unadjusted?.mean ?? p.mean, ci: p.primary_unadjusted?.ci ?? p.ci }));
  const experiment = { ...d.experiment, adjustment: null, primary_unadjusted: null, primary_mean: primary.mean };
  return { ...d, spec: { ...d.spec, analysis: null }, experiment, report: { ...r, analysis: null, primary, pairwise } as ExperimentReport };
}

describe("Experiments page: SPY-adjusted headline (D120)", () => {
  it("K=3 pre-registered: adjusted hero, unadjusted second line, a panel row per arm", () => {
    const html = render(K3_D119_DETAIL);
    expect(html).toContain("SPY-adjusted (flat-market day)");
    expect(text(html, "result-hero")[0]).toBe("+0.40%/day");
    expect(text(html, "result-second")[0]).toBe("Unadjusted: +0.47% [−0.00, +0.95] · p 0.05");
    expect(html).not.toContain('data-testid="verdict-note"');
    expect(text(html, "pair-headline")).toHaveLength(3);
    expect(html).toContain("β̂ 0.57 ± 0.02");
    expect(html).toContain("HAC f 1.0 (running)");
    expect(html.match(/data-testid="mostly-beta"/g)).toHaveLength(1);
  });

  it("pre-D119 report: no panel, no second line, the hero is the D44 mean", () => {
    const html = render(preD119(K3_D119_DETAIL));
    expect(html).not.toContain('data-testid="adjustment-blocks"');
    expect(html).not.toContain('data-testid="result-second"');
    expect(html).not.toContain("SPY-adjusted");
    expect(html).toContain("paired daily P&amp;L");
    expect(text(html, "result-hero")[0]).toBe("+0.47%/day");
  });
});
