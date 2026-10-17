// D124: backtests on the Experiments page. The model formats stored ledger numbers only;
// the list row and the detail page render a real BT-7 body.
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import {
  bestOf,
  callPill,
  headlineCaption,
  headlineTone,
  isBacktestId,
  leadRun,
  metricRows,
  money,
  overlayLines,
  parseFilter,
  periodText,
  profileOrder,
  rangeText,
  retiredText,
  runsCaption,
  splitRetired,
  stageRows,
  summaryStrip,
} from "../lib/backtests";
import { BT7_DETAIL, BT_LIST } from "../lib/backtests.fixture";
import { BacktestBody, BacktestListRow } from "./Backtests";

function render(node: React.ReactNode): string {
  return renderToStaticMarkup(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>{node}</MemoryRouter>
    </QueryClientProvider>,
  );
}

const plain = (html: string) => html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");

describe("backtests model", () => {
  it("money: compact, signed, true minus", () => {
    expect(money(35012.4)).toBe("+$35.0K");
    expect(money(-923.23)).toBe("−$923");
    expect(money(-1_250_000)).toBe("−$1.3M");
    expect(money(0)).toBe("$0");
    expect(money(null)).toBe("—");
    expect(money(300, false)).toBe("$300");
  });

  it("tone only when the whole range is one side of zero", () => {
    expect(headlineTone({ ci_lo: 3500, ci_hi: 69400 })).toBe("pos");
    expect(headlineTone({ ci_lo: -10, ci_hi: -1 })).toBe("neg");
    expect(headlineTone({ ci_lo: -47500, ci_hi: 50200 })).toBe("neutral");
  });

  it("list row text: the live account's profile, no sim length, no second profile", () => {
    const r = BT_LIST.items.find((i) => i.study_id === "BT-7")!;
    expect(r.profile).toBe("cash_debit");
    expect(headlineCaption(r)).toBe("T1 vs Control · Cash Debit");
    expect(callPill(r.headline).label).toBe("Keep Control");
    expect(rangeText(r.headline)).toMatch(/^\[−\$47\.\dK, \+\$50\.\dK\]$/);
    expect(runsCaption(r)).toBe("Last run 10/16 · 3 runs");
  });

  it("summary strip counts live and backtests separately", () => {
    const live = [{ experiment_id: "XP-1", status: "running", verdict: "continue", sessions: 3, max_sessions: 10 }];
    const s = summaryStrip(live, BT_LIST);
    expect(s.map((x) => x.label)).toEqual(["Live Running", "Live Decided", "Backtests Done", "Backtest Switches", "Queue", "Tonight"]);
    expect(s[0]).toMatchObject({ value: "1", sub: "XP-1 · day 3/10" });
    expect(s[2]!.value).toBe("1 / 2");
    expect(s[3]!.sub).toBe("on Cash Debit holdout");
    expect(s[5]).toMatchObject({ value: "Idle", sub: "next slot 21:30 ET" });
  });

  it("detail helpers: holdout leads, live profile first, ladder rows oldest first", () => {
    const run = leadRun(BT7_DETAIL)!;
    expect(run.stage).toBe("C500 Holdout");
    expect(periodText(run)).toBe("2.8 years");
    expect(profileOrder(BT7_DETAIL)).toEqual(["cash_debit", "margin"]);
    expect(bestOf(run, "margin")!.pnl_diff).toBeGreaterThan(30000);
    expect(stageRows(BT7_DETAIL, "cash_debit").map((r) => r.stage)).toEqual(["C125 Dev", "C500 Dev", "C500 Holdout"]);
  });

  it("metric rows tone by which way is better", () => {
    const res = leadRun(BT7_DETAIL)!.results.find((r) => r.profile === "cash_debit")!;
    const rows = metricRows(res.arms[0], res.arms[1]);
    const dd = rows.find((r) => r.label === "Max Drawdown")!;
    expect(dd.tone).toBe("neg"); // drawdown went up
    expect(rows.find((r) => r.label === "Trades")!.tone).toBe("neutral");
  });

  it("ids, filter and overlay lines", () => {
    expect(isBacktestId("BT-7")).toBe(true);
    expect(isBacktestId("XP-7")).toBe(false);
    expect(isBacktestId("BT-0")).toBe(false);
    expect(parseFilter("backtest")).toBe("backtest");
    expect(parseFilter("x")).toBe("all");
    expect(overlayLines({ regime: { version: 2 } })).toEqual(["regime.version = 2"]);
    expect(overlayLines({})).toEqual(["No change (same as control)"]);
  });
});

describe("backtest list row", () => {
  it("pills on the top line, full summary (clamped to 2 lines), two bars, $ under the range", () => {
    const r = BT_LIST.items.find((i) => i.study_id === "BT-7")!;
    const html = render(<BacktestListRow r={r} />);
    expect(html).toContain('data-testid="row-top"');
    expect(html).toMatch(/data-testid="row-right".*Keep Control.*Open ↗/);
    expect(html).toMatch(/line-clamp-2[^"]*" data-testid="row-summary"/);
    expect(html).toContain('data-testid="ladder-bar"');
    expect(html).toContain('data-testid="row-range"');
    expect(html).toMatch(/data-testid="row-value".*−\$923/);
    const t = plain(html);
    expect(t).not.toMatch(/\bsim\b|Margin|Backtest\b|Live\b|D\d+|E\d+\.\d/);
  });
});

describe("backtest detail page", () => {
  it("leads with the live account's profile and switches to Margin", () => {
    const html = render(<BacktestBody d={BT7_DETAIL} />);
    expect(html).toContain('data-testid="profile-switch"');
    expect(plain(html)).toContain("Cash Debit Margin");
    expect(html).toMatch(/data-testid="bt-result-hero"[^>]*>−\$923/);
    expect(plain(html)).toContain("on Cash Debit (live account)");
    expect(html).toContain('data-testid="bt-stages"');
    expect(html).toContain('data-testid="bt-metrics"');
    expect(html).toContain('data-testid="bt-linked-empty"');
    expect(html).not.toContain('data-testid="retired-banner"');
  });

  it("a retired study opens with the retired banner and reason", () => {
    const d = { ...BT7_DETAIL, study: { ...BT7_DETAIL.study, retired: { at: "2026-10-17", reason: "harness evolved" } } };
    const html = render(<BacktestBody d={d} />);
    expect(html).toContain('data-testid="retired-banner"');
    expect(plain(html)).toContain("Retired 2026-10-17: harness evolved. History only");
  });
});

describe("retired backtests", () => {
  const RETIRED = { at: "2026-10-17", reason: "harness evolved" };

  it("split: retired leave the current list and the summary strip counts", () => {
    const items = BT_LIST.items.map((i) => (i.study_id === "BT-7" ? { ...i, retired: RETIRED } : i));
    const { current, retired } = splitRetired(items);
    expect(retired.map((i) => i.study_id)).toEqual(["BT-7"]);
    expect(current.some((i) => i.study_id === "BT-7")).toBe(false);
    expect(summaryStrip([], { ...BT_LIST, items })[2]!.value).toBe(`0 / ${current.length}`);
    expect(retiredText({ retired: RETIRED })).toBe("Retired 2026-10-17: harness evolved");
    expect(retiredText({ retired: null })).toBeNull();
  });

  it("row carries the Retired pill and the reason", () => {
    const r = { ...BT_LIST.items.find((i) => i.study_id === "BT-7")!, retired: RETIRED };
    const html = render(<BacktestListRow r={r} />);
    expect(html).toContain('data-testid="retired-pill"');
    expect(plain(html)).toContain("Retired 2026-10-17: harness evolved");
  });
});
