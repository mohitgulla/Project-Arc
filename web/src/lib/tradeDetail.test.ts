// E8.8f (D48): the trade detail page model. Risk narratives below are real live rows
// (data/arc.db, 2026-10-02/03), trimmed only where noted.
import { describe, expect, it } from "vitest";

import {
  defaultTab,
  focusStage,
  lifecycleStages,
  parseRiskNarrative,
  parseTab,
  sentences,
  splitTrail,
  statStrip,
  TABS,
} from "./tradeDetail";
import type { TradeDetail, TradeStage } from "./trades";

const GS =
  "This is a defined-risk bear put debit vertical (long 930P / short 855P, Nov 20). Debit and max loss are $3,125.50 per contract, max gain is $4,374.50, and the breakeven is 898.75. " +
  "The gate formula allows floor(8% x $101,185 / $3,125.50) = 2, and the structure cap is also 2. I advise 1 contract, about 3.1% of equity at risk. " +
  "Reasons: (1) GS earnings timing is unknown and almost certainly inside the window. (2) Quant confidence is only 0.45, below Research's 0.48 bar. " +
  "(3) EV depends on the bear drift continuing, with a 0.29 stop probability against a 0.43 take-profit probability. (4) Execution cost is 828 bps, so a poor fill erodes the +$514 managed EV. " +
  "(5) DTE is outside the mandate. With 1 lot, combined book max loss is about $7,782 (MU $4,656.50 + GS $3,125.50), about 7.7% of equity. " +
  "Managed metrics are acceptable: PoP 0.567, managed EV +$513.73, rorc_day 0.0052, expected hold about 31 days. Work the order at or inside mid. " +
  "If the earnings date is confirmed before CPI or near entry, consider waiting until after the print or declining. Advisory only. The deterministic risk gate decides final size.";

const MU =
  "This is the anchor trade, and the managed numbers are good: managed EV +$408, rorc/day 0.0041 and managed PoP 0.47. Max loss is $4,635.50, or 4.6% of equity. " +
  "That limit allows only 1 lot (floor(8,000/4,635.5) = 1), and the cap is also 1, so 1 lot is the only possible size. " +
  "Recommendations: (1) confirm MU's next earnings date before entry; (2) enter after the Oct 2 NFP print, as Quant suggested; (3) confirm that the $4,635.50 debit plus fees fits in settled cash. " +
  "Sizing is advisory; the deterministic gate decides.";

const NFLX =
  "Suggested size is 5 contracts (advisory): max loss $965, or 0.96% of $100,061.65 equity. Sizing math for the gate: floor(8% x equity / $193) = 41, and the structure cap is 38. " +
  "Conditions before entry: (a) confirm the NFLX earnings date, since the feed shows it as unknown; (b) enter after NFP on Oct 2 at a debit of $1.93 or less on fresh chains, and size down if the fill is worse; (c) confirm settled cash covers about $965 plus fees with no margin. " +
  "This sizing is advisory only. The deterministic risk gate has final authority.";

const PLAIN = "Defined risk 1.2% of equity; no concentration.";

/** Every number in *s* (digits with separators), to prove the parse drops none. */
const numbers = (s: string): string[] => [...(s.match(/\d[\d,]*(?:\.\d+)?/g) ?? [])].sort();

describe("parseRiskNarrative (structured)", () => {
  it("GS: sizing box, numbered reasons, closing advice", () => {
    const v = parseRiskNarrative(GS);
    expect(v.kind).toBe("structured");
    if (v.kind !== "structured") return;
    expect(v.facts).toEqual([
      { label: "Formula allows", value: "floor(8% x $101,185 / $3,125.50) = 2" },
      { label: "Structure cap", value: "2" },
      { label: "Suggested size", value: "1 contract" },
    ]);
    expect(v.list?.label).toBe("Reasons");
    expect(v.list?.items).toHaveLength(5);
    expect(v.list?.items[0]).toBe("GS earnings timing is unknown and almost certainly inside the window");
    expect(v.list?.items[4]).toBe("DTE is outside the mandate");
    expect(v.advice).toMatch(/^Work the order at or inside mid\. .*final size\.$/);
    expect(v.summary).toContain("With 1 lot, combined book max loss is about $7,782");
    expect(v.summary).not.toContain("(1)");
  });

  it("MU: lettered/numbered recommendations split on semicolons", () => {
    const v = parseRiskNarrative(MU);
    if (v.kind !== "structured") throw new Error("expected structured");
    expect(v.list?.label).toBe("Recommendations");
    expect(v.list?.items).toEqual([
      "confirm MU's next earnings date before entry",
      "enter after the Oct 2 NFP print, as Quant suggested",
      "confirm that the $4,635.50 debit plus fees fits in settled cash",
    ]);
    expect(v.facts.find((f) => f.label === "Suggested size")?.value).toBe("1 lot");
    expect(v.advice).toBe("Sizing is advisory; the deterministic gate decides.");
  });

  it("NFLX: (a)…(c) conditions and the explicit structure cap", () => {
    const v = parseRiskNarrative(NFLX);
    if (v.kind !== "structured") throw new Error("expected structured");
    expect(v.list?.label).toBe("Conditions before entry");
    expect(v.list?.items).toHaveLength(3);
    expect(v.facts).toContainEqual({ label: "Structure cap", value: "38" });
    expect(v.facts).toContainEqual({ label: "Suggested size", value: "5 contracts" });
    expect(v.advice).toBe("This sizing is advisory only. The deterministic risk gate has final authority.");
  });

  it.each([GS, MU, NFLX])("drops no number: summary + list + advice hold every number", (raw) => {
    const v = parseRiskNarrative(raw);
    if (v.kind !== "structured") throw new Error("expected structured");
    const kept = numbers([v.summary, ...(v.list?.items ?? []), v.advice].join(" "));
    // The list markers `(1)…(n)` are the only digits allowed to disappear.
    const lost = numbers(raw).filter((x) => !kept.includes(x));
    expect(lost.every((x) => /^[1-5]$/.test(x))).toBe(true);
  });
});

describe("parseRiskNarrative (fallback)", () => {
  it("no list and no sizing fact: the original paragraph", () => {
    expect(parseRiskNarrative(PLAIN)).toEqual({ kind: "text", text: PLAIN });
  });

  it("empty / null: empty text", () => {
    expect(parseRiskNarrative("")).toEqual({ kind: "text", text: "" });
    expect(parseRiskNarrative(null)).toEqual({ kind: "text", text: "" });
  });

  it("a lone (1) with no (2) is not a list", () => {
    const raw = "Reasons: (1) only one reason here. Nothing else.";
    expect(parseRiskNarrative(raw)).toEqual({ kind: "text", text: raw });
  });
});

describe("sentences", () => {
  it("never splits inside a decimal or a dollar amount", () => {
    expect(sentences("Max loss is $3,125.50 per lot. PoP 0.567 is fine.")).toEqual([
      "Max loss is $3,125.50 per lot.",
      "PoP 0.567 is fine.",
    ]);
  });
});

describe("tabs", () => {
  const cases: Array<[TradeStage, string]> = [
    ["proposed", "why"],
    ["gate_pass", "why"],
    ["approved", "why"],
    ["filled", "why"],
    ["open", "why"],
    ["closed", "lifecycle"],
    ["gate_fail", "lifecycle"],
    ["rejected", "lifecycle"],
    ["expired", "lifecycle"],
    ["cancelled", "lifecycle"],
  ];
  it.each(cases)("default tab for %s is %s", (stage, tab) => {
    expect(defaultTab(stage)).toBe(tab);
  });

  it("parseTab accepts the five tabs only", () => {
    expect(TABS.map((t) => t.label)).toEqual(["Why", "Numbers", "Lifecycle", "Context", "Audit"]);
    for (const t of TABS) expect(parseTab(t.value)).toBe(t.value);
    expect(parseTab("quant")).toBeNull();
    expect(parseTab(null)).toBeNull();
  });
});

describe("splitTrail", () => {
  it("separates this trade's steps from chain context, keeping order", () => {
    const items = [
      { id: "a", this_trade: false },
      { id: "b", this_trade: true },
      { id: "c", this_trade: false },
      { id: "d", this_trade: true },
    ];
    const s = splitTrail(items);
    expect(s.own.map((i) => i.id)).toEqual(["b", "d"]);
    expect(s.chain.map((i) => i.id)).toEqual(["a", "c"]);
  });
});

// A detail payload with only the fields the strip reads (cast: the rest is irrelevant here).
function detail(over: { realized?: number | null; mark?: number | null } = {}): TradeDetail {
  return {
    header: { dte: 24, row: { realized_pnl: over.realized ?? null } },
    payoff: { mark_pnl: over.mark ?? null },
    quant: {
      net_ev_managed: 12.4,
      pop_managed: 0.54,
      max_gain: "6.10",
      max_loss: "3.90",
      cost_bps: 35,
      dte: 24,
    },
  } as unknown as TradeDetail;
}

describe("statStrip: parity with the Numbers tab's API fields", () => {
  it("reads net EV / PoP managed, max gain / loss, cost, DTE straight from the API", () => {
    const d = detail({ mark: 87.5 });
    const s = Object.fromEntries(statStrip(d).map((x) => [x.key, x]));
    expect(s.net_ev!.value).toBe(d.quant.net_ev_managed);
    expect(s.pop!.value).toBe(d.quant.pop_managed);
    expect(s.max!.value).toBe(Number(d.quant.max_gain));
    expect(s.max!.value2).toBe(Number(d.quant.max_loss));
    expect(s.cost!.value).toBe(d.quant.cost_bps);
    expect(s.dte!.value).toBe(d.header.dte);
    expect(s.pnl).toMatchObject({ label: "P&L now", value: 87.5 });
  });

  it("P&L becomes realised once closed", () => {
    const s = statStrip(detail({ realized: 300, mark: 87.5 })).find((x) => x.key === "pnl");
    expect(s).toMatchObject({ label: "Realized P&L", value: 300 });
  });

  it("six cells, missing values stay null (rendered as a dash)", () => {
    const d = { ...detail(), quant: {}, header: { row: {} }, payoff: {} } as unknown as TradeDetail;
    const s = statStrip(d);
    expect(s).toHaveLength(6);
    expect(s.every((x) => x.value === null)).toBe(true);
  });
});

// A lifecycle payload with only the fields lifecycleStages reads.
function life(over: Record<string, unknown>): TradeDetail {
  return {
    header: { row: { stage: "proposed" } },
    gate: [],
    approval: null,
    execution: null,
    position: null,
    outcome: { outcome: null, reviews: [] },
    ...over,
  } as unknown as TradeDetail;
}

describe("lifecycleStages / focusStage", () => {
  it("gate FAIL + not_actionable approval: the gate is the focus, execution not reached", () => {
    const s = lifecycleStages(life({ gate: [{ passed: false }], approval: { status: "not_actionable" } }));
    expect(s.map((x) => x.state)).toEqual(["failed", "failed", "pending", "pending", "pending"]);
    expect(focusStage(s)).toBe("gate");
  });

  it("filled open position: latest reached stage (position) is the focus and still active", () => {
    const s = lifecycleStages(
      life({
        header: { row: { stage: "open" } },
        gate: [{ passed: true }],
        approval: { status: "approved" },
        execution: { status: "filled" },
        position: { structure_id: "s1", status: "open", exits: [], swaps: [] },
      }),
    );
    expect(s.map((x) => x.state)).toEqual(["done", "done", "done", "active", "pending"]);
    expect(focusStage(s)).toBe("position");
  });

  it("working order and pending approval read as active; nothing reached → no focus", () => {
    const s = lifecycleStages(life({ gate: [{ passed: true }], approval: { status: "pending" }, execution: { status: "working" } }));
    expect(s.slice(1, 3).map((x) => x.state)).toEqual(["active", "active"]);
    expect(focusStage(lifecycleStages(life({})))).toBeUndefined();
  });
});
