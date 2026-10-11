/**
 * Trade detail page model (E8.8f, D48): tabs, the default tab by stage, the sticky stat strip
 * and the presentation-only parse of Risk's narrative. Pure functions, unit-tested in
 * tradeDetail.test.ts. Nothing here changes what is stored or what the LLM is asked.
 */
import type { TradeDetail, TradeStage } from "./trades";

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------

export const TABS = [
  { value: "why", label: "Why" },
  { value: "numbers", label: "Numbers" },
  { value: "lifecycle", label: "Lifecycle" },
  { value: "context", label: "Context" },
  { value: "audit", label: "Audit" },
] as const;
export type TabKey = (typeof TABS)[number]["value"];

/** Stages where the trade is finished: the story is what happened, so Lifecycle leads. */
const FINISHED: ReadonlySet<TradeStage> = new Set(["closed", "gate_fail", "rejected", "expired", "cancelled"]);

/** Why for live trades (proposed → open), Lifecycle once the trade is finished. */
export function defaultTab(stage: TradeStage): TabKey {
  return FINISHED.has(stage) ? "lifecycle" : "why";
}

/** `?tab=` → a known tab, else null (the caller falls back to `defaultTab`). */
export function parseTab(raw: string | null | undefined): TabKey | null {
  return TABS.some((t) => t.value === raw) ? (raw as TabKey) : null;
}

// ---------------------------------------------------------------------------
// Stat strip (sticky summary)
// ---------------------------------------------------------------------------

export type StatKey = "net_ev" | "pop" | "max" | "cost" | "dte" | "pnl";

export interface Stat {
  key: StatKey;
  label: string;
  /** Raw API numbers; the component formats them (no formatting in the model). */
  value: number | null;
  /** Second number of a pair (max loss beside max gain). */
  value2?: number | null;
  hint?: string;
}

const n = (v: string | number | null | undefined): number | null => {
  if (v === null || v === undefined || v === "") return null;
  const x = typeof v === "number" ? v : Number(v);
  return Number.isFinite(x) ? x : null;
};

/**
 * The six summary numbers, read straight from the same API fields the Numbers tab shows
 * (parity is tested): Net EV and PoP under managed exits, max gain / loss per unit, cost in
 * bps, DTE at proposal, and P&L (realised once closed, else the latest mark).
 */
export function statStrip(d: TradeDetail): Stat[] {
  const q = d.quant;
  const realized = n(d.header.row.realized_pnl);
  const mark = n(d.payoff.mark_pnl);
  return [
    { key: "net_ev", label: "Net EV (managed)", value: n(q.net_ev_managed), hint: "per unit, after all costs" },
    { key: "pop", label: "PoP (managed)", value: n(q.pop_managed) },
    { key: "max", label: "Max gain / loss", value: n(q.max_gain), value2: n(q.max_loss), hint: "per unit" },
    { key: "cost", label: "Cost (bps)", value: n(q.cost_bps) },
    { key: "dte", label: "DTE", value: n(d.header.dte ?? q.dte), hint: "at proposal" },
    realized !== null
      ? { key: "pnl", label: "Realized P&L", value: realized }
      : { key: "pnl", label: "P&L now", value: mark, hint: mark === null ? undefined : "latest broker mark" },
  ];
}

// ---------------------------------------------------------------------------
// Decision trail split
// ---------------------------------------------------------------------------

/** This trade's own steps vs the chain context rows (other tickers / the session). */
export function splitTrail<T extends { this_trade: boolean }>(items: readonly T[]): { own: T[]; chain: T[] } {
  return { own: items.filter((i) => i.this_trade), chain: items.filter((i) => !i.this_trade) };
}

// ---------------------------------------------------------------------------
// Risk narrative → structured blocks (presentation only, safe fallback)
// ---------------------------------------------------------------------------

export interface RiskFact {
  label: string;
  value: string;
}

export interface RiskList {
  /** The intro label as written (`Reasons`, `Recommendations`, `Conditions before entry`). */
  label: string;
  items: string[];
}

export type RiskView =
  | { kind: "structured"; facts: RiskFact[]; summary: string; list: RiskList | null; advice: string }
  | { kind: "text"; text: string };

/** Sentence boundaries: `.`/`!`/`?` then space then a capital or `(`; `$3,125.50` never splits. */
const SENTENCE_END = /(?<=[.!?])\s+(?=[A-Z(])/;

export function sentences(text: string): string[] {
  return text
    .split(SENTENCE_END)
    .map((s) => s.trim())
    .filter(Boolean);
}

/** A closing sentence is advice when it mentions the gate, advisory sizing or what to do next. */
const ADVICE = /\b(advis\w*|gate|consider|work the order|declin\w*|wait\w*)\b/i;

const LIST_INTRO = /([A-Z][A-Za-z ,'-]{0,40}?):\s*\((1|a)\)\s+/;

function nextMarker(m: string): string {
  return /^\d+$/.test(m) ? String(Number(m) + 1) : String.fromCharCode(m.charCodeAt(0) + 1);
}

function cleanItem(s: string): string {
  return s
    .trim()
    .replace(/^and\s+/i, "")
    .replace(/[;,.]\s*(and)?$/i, "")
    .trim();
}

/** `Label: (1) … (2) … (n) …` with ≥ 2 items; the last item ends at its sentence end. */
function extractList(text: string): { before: string; list: RiskList; after: string } | null {
  const m = LIST_INTRO.exec(text);
  if (!m) return null;
  const items: string[] = [];
  let marker = m[2]!;
  let pos = m.index + m[0].length;
  for (;;) {
    const next = `(${nextMarker(marker)}) `;
    const at = text.indexOf(next, pos);
    // A following marker must be close (one item), else the list ended.
    if (at < 0 || at - pos > 600) break;
    items.push(text.slice(pos, at));
    pos = at + next.length;
    marker = nextMarker(marker);
  }
  const rest = text.slice(pos);
  const end = rest.search(/[.!?](?=\s+[A-Z(]|\s*$)/);
  const last = end < 0 ? rest : rest.slice(0, end + 1);
  items.push(last);
  if (items.length < 2) return null;
  return {
    before: text.slice(0, m.index).trim(),
    list: { label: m[1]!.trim(), items: items.map(cleanItem).filter(Boolean) },
    after: (end < 0 ? "" : rest.slice(end + 1)).trim(),
  };
}

/** Sizing facts that are unambiguous in Risk's prose (the gate formula, cap, suggestion). */
function extractFacts(text: string): RiskFact[] {
  const facts: RiskFact[] = [];
  const formula = /floor\s*\(([^()]*)\)\s*=\s*(\d+)/i.exec(text);
  if (formula) facts.push({ label: "Formula allows", value: `floor(${formula[1]!.trim()}) = ${formula[2]}` });
  const cap = /\bstructure cap is (?:also )?(\d+)\b/i.exec(text) ?? /\bcapped at (\d+)\b/i.exec(text);
  if (cap) facts.push({ label: "Structure cap", value: cap[1]! });
  const size =
    /\b(?:I (?:advise|suggest|recommend)|Suggested size is)\s+(\d+\s+(?:contracts?|lots?))\b/i.exec(text) ??
    /\b(\d+\s+(?:contracts?|lots?)) is the only possible size\b/i.exec(text);
  if (size) facts.push({ label: "Suggested size", value: size[1]! });
  return facts;
}

/**
 * Split Risk's narrative into a sizing KeyValue box, the enumerated list (`Reasons: (1)…`) and
 * the closing advice callout. Everything that is not the list or the advice stays in `summary`
 * verbatim, so no number is dropped. Falls back to the original paragraph when neither a list
 * nor a sizing fact is found.
 */
export function parseRiskNarrative(raw: string | null | undefined): RiskView {
  const text = (raw ?? "").replace(/\s+/g, " ").trim();
  if (!text) return { kind: "text", text: "" };
  const facts = extractFacts(text);
  const found = extractList(text);
  if (!found && facts.length === 0) return { kind: "text", text };
  // Advice is the closing run of sentences: after the list when there is one, else the end.
  const head = found ? found.before : "";
  const tail = sentences(found ? found.after : text);
  const advice: string[] = [];
  const keep = found ? 0 : 1;
  while (tail.length > keep && ADVICE.test(tail[tail.length - 1]!)) advice.unshift(tail.pop()!);
  return {
    kind: "structured",
    facts,
    summary: [head, ...tail].filter(Boolean).join(" "),
    list: found ? found.list : null,
    advice: advice.join(" "),
  };
}

/** Thesis author's persona key: research for opens, quant for exits (D22; D56). The
 * label comes from the /api/meta catalogue. */
export function thesisPersona(kind: "open" | "close"): "research" | "quant" {
  return kind === "close" ? "quant" : "research";
}

// ---------------------------------------------------------------------------
// Lifecycle timeline (Gate → Approval → Execution → Position & exits → Outcome & review)
// ---------------------------------------------------------------------------

export type LifeKey = "gate" | "approval" | "execution" | "position" | "outcome";
export type LifeState = "done" | "failed" | "active" | "pending";

export interface LifeStage {
  key: LifeKey;
  title: string;
  state: LifeState;
  /** Plain one-line result; numbers are formatted by the component from the payload. */
  reached: boolean;
}

const FAILED_APPROVAL = new Set(["rejected", "expired", "not_actionable"]);
const FAILED_EXEC = new Set(["cancelled", "rejected", "unconfirmed"]);

/** Which lifecycle stages a trade reached, and how each ended (not reached → pending/muted). */
export function lifecycleStages(d: TradeDetail): LifeStage[] {
  const g = d.gate.length ? d.gate[d.gate.length - 1] : undefined;
  const a = d.approval;
  const x = d.execution;
  const p = d.position;
  const o = d.outcome;
  const hasPos = !!p && (!!p.structure_id || (p.exits ?? []).length > 0 || (p.swaps ?? []).length > 0);
  const hasOutcome = !!o.outcome || (o.reviews ?? []).length > 0;
  const closed = d.header.row.stage === "closed" || p?.status === "closed";
  const st = (reached: boolean, failed: boolean, finished: boolean): LifeState =>
    !reached ? "pending" : failed ? "failed" : finished ? "done" : "active";
  return [
    { key: "gate", title: "Gate", reached: !!g, state: st(!!g, !!g && !g.passed, true) },
    {
      key: "approval",
      title: "Approval",
      reached: !!a,
      state: st(!!a, !!a && FAILED_APPROVAL.has(a.status), !!a && a.status !== "pending"),
    },
    {
      key: "execution",
      title: "Execution",
      reached: !!x,
      state: st(!!x, !!x && FAILED_EXEC.has(x.status ?? ""), !!x && x.status !== "working"),
    },
    { key: "position", title: "Position & Exits", reached: hasPos, state: st(hasPos, false, closed) },
    { key: "outcome", title: "Outcome & Review", reached: hasOutcome, state: st(hasOutcome, false, true) },
  ];
}

/** The stage the Lifecycle tab opens expanded: the first failure, else the latest reached. */
export function focusStage(stages: readonly LifeStage[]): LifeKey | undefined {
  return stages.find((s) => s.state === "failed")?.key ?? [...stages].reverse().find((s) => s.reached)?.key;
}

/** Where each block's numbers come from (moved off the section headers into Audit). */
export const SOURCES: ReadonlyArray<{ block: string; tables: string }> = [
  { block: "Payoff", tables: "proposals.structure_json (arc.structures)" },
  { block: "Quant", tables: "proposals.quant_json, market_contexts.analytics" },
  { block: "Decision trail", tables: "decisions, persona_calls" },
  { block: "Gate", tables: "gate_decisions" },
  { block: "Approval", tables: "approval_requests" },
  { block: "Execution", tables: "executions, orders, order_events, fills" },
  { block: "Position & exits", tables: "open_structures, proposals (kind=close), swaps" },
  { block: "Outcome & review", tables: "outcomes, decision_reviews" },
  { block: "Market context", tables: "market_contexts, context_snapshots, context_entries, candidates" },
  { block: "Run manifest", tables: "run_manifests (D27)" },
];

/** E16.5: each breakeven's distance in ATR14·√DTE ("1.1 / 1.1 ATR√t"); undefined without ATR. */
export function beAtrHint(bes: { atr_multiple?: number | null }[] | null | undefined): string | undefined {
  const vals = (bes ?? []).map((b) => b.atr_multiple).filter((v): v is number => v != null);
  return vals.length ? `${vals.map((v) => v.toFixed(1)).join(" / ")} ATR√t` : undefined;
}
