// E8.8e: pure helpers for the full-page Effective Config / Change Log (/ops/config) and the
// concise run detail (/ops/runs/:runId). Kept apart from lib/ops.ts so each page has one lib.
import type { components } from "./api.gen";
import type { OpsConfig, StepView } from "./ops";

type Schemas = components["schemas"];
export type ConfigKey = Schemas["ConfigKeyRow"];
export type ConfigChange = Schemas["ConfigChangeRow"];
export type ConfigGroup = Schemas["ConfigGroupRow"];

// -- actors ----------------------------------------------------------------------------

/** Slack id -> display name from `tower.actor_names`; an unknown id (or "cli") shows as is. */
export function actorName(actor: string | null | undefined, names: Record<string, string> | null | undefined): string {
  if (!actor) return "—";
  return names?.[actor] ?? actor;
}

/** `slack` -> Slack, `cli` -> CLI; anything else capitalised. */
export function sourceLabel(source: string): string {
  if (source === "cli") return "CLI";
  if (source === "slack") return "Slack";
  return source ? source[0]!.toUpperCase() + source.slice(1) : "—";
}

// -- list values -----------------------------------------------------------------------

/** Plural noun for a list key's count: `60 tickers`, `2 ids`, `3 items`. */
export function listNoun(valueType: string | null | undefined, n: number): string {
  const word = valueType === "tickers" ? "ticker" : valueType === "user_ids" ? "id" : "item";
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}

export function asList(v: unknown): string[] {
  if (Array.isArray(v)) return v.map((x) => String(x));
  return [];
}

/** Members added / removed between two lists, each in the list's own order. */
export function listDiff(oldV: unknown, newV: unknown): { added: string[]; removed: string[] } {
  const o = asList(oldV);
  const n = asList(newV);
  const os = new Set(o);
  const ns = new Set(n);
  return { added: n.filter((x) => !os.has(x)), removed: o.filter((x) => !ns.has(x)) };
}

/** `+SMH +SCHD −DIA −XLF` (U+2212 minus, like the Slack `!arc config diff`). */
export function listDiffText(oldV: unknown, newV: unknown): string {
  const d = listDiff(oldV, newV);
  return [...d.added.map((x) => `+${x}`), ...d.removed.map((x) => `\u2212${x}`)].join(" ") || "no member change";
}

/** One change's headline after the key: `20 → 100 tickers` for lists, `old → new` for scalars. */
export function changeHeadline(ch: ConfigChange, valueType?: string | null): string {
  const newText = ch.is_default ? `default${ch.new_text != null ? ` (${ch.new_text})` : ""}` : (ch.new_text ?? "—");
  if (ch.is_list) {
    const vt = valueType ?? (ch.group === "universe" ? "tickers" : null);
    const o = asList(ch.old).length;
    const n = asList(ch.new).length;
    return ch.is_default ? `${o} → ${newText}` : `${o} → ${listNoun(vt, n)}`;
  }
  return `${ch.old_text ?? "—"} → ${newText}`;
}

// -- key rows --------------------------------------------------------------------------

/**
 * Allowed values: the registry's own bounds text (`0.5% – 10%`, `$1.00 – $25.00`,
 * `a | b | c`, `on | off`); list keys with no bounds read `any tickers` / `any ids`.
 */
export function allowedText(k: Pick<ConfigKey, "bounds" | "value_type" | "choices">): string {
  if (k.choices && k.choices.length) return k.choices.join(" | ");
  if (k.bounds && k.bounds !== "-") return k.bounds;
  if (k.value_type === "tickers") return "any tickers";
  if (k.value_type === "user_ids") return "subset of the base ids";
  if (k.value_type === "targets") return "DTE:fraction pairs or none";
  if (k.value_type === "profit_lock") return "arm:floor[:eod] fractions or none";
  if (k.value_type === "cadence") return "every Nm [HH:MM-HH:MM] | at HH:MM";
  return "any";
}

/** Plain words for the risk direction (which way a change is riskier). */
export const RISK_LABEL: Record<string, string> = {
  up: "higher is riskier",
  down: "lower is riskier",
  order: "later option is riskier",
  true: "on is riskier",
  false: "off is riskier",
  grow: "adding is riskier",
  neutral: "neutral",
};

export function riskLabel(risk: string): string {
  return RISK_LABEL[risk] ?? risk;
}

/** Split a dotted key into segments that may wrap after each `.` (keys are never truncated). */
export function keySegments(key: string): string[] {
  const parts = key.split(".");
  return parts.map((p, i) => (i < parts.length - 1 ? `${p}.` : p));
}

/** A key matches the page filter on its key, group, description or value text (case-insensitive). */
export function keyMatches(k: ConfigKey, q: string): boolean {
  const s = q.trim().toLowerCase();
  if (!s) return true;
  return [k.key, k.group, k.description, k.value_text].some((x) => (x ?? "").toLowerCase().includes(s));
}

export interface ConfigSection {
  key: string;
  label: string;
  keys: ConfigKey[];
}

/**
 * Sections in the API's registry group order (`groups`), each with the keys that pass the
 * filter; overridden keys first within a section, else registry order. Empty sections drop.
 */
export function configSections(c: Pick<OpsConfig, "keys" | "groups">, q = "", overridesOnly = false): ConfigSection[] {
  const groups = c.groups?.length ? c.groups : [...new Set(c.keys.map((k) => k.group))].map((g) => ({ key: g, label: g, keys: 0 }));
  const known = new Set(groups.map((g) => g.key));
  const extra = [...new Set(c.keys.map((k) => k.group).filter((g) => !known.has(g)))].map((g) => ({ key: g, label: g, keys: 0 }));
  return [...groups, ...extra]
    .map((g) => ({
      key: g.key,
      label: g.label,
      keys: c.keys
        .filter((k) => k.group === g.key && keyMatches(k, q) && (!overridesOnly || k.source === "override"))
        .sort((a, b) => Number(b.source === "override") - Number(a.source === "override")),
    }))
    .filter((s) => s.keys.length > 0);
}

/** Change-log rows that pass the filter (key, actor id or name, reason). */
export function changeMatches(ch: ConfigChange, q: string, names?: Record<string, string> | null): boolean {
  const s = q.trim().toLowerCase();
  if (!s) return true;
  return [ch.key, ch.actor, actorName(ch.actor, names), ch.reason ?? "", ch.group ?? ""].some((x) => x.toLowerCase().includes(s));
}

export const CHANGE_PAGE = 20;

// -- run detail ------------------------------------------------------------------------

export interface KindCount {
  kind: string;
  n: number;
  undeclared: boolean;
}

/** Context refs as counts per kind: undeclared kinds first, then by count, then by name. */
export function kindCounts(items: StepView["read"]): KindCount[] {
  const by = new Map<string, KindCount>();
  for (const e of items) {
    const k = by.get(e.kind) ?? { kind: e.kind, n: 0, undeclared: false };
    k.n += 1;
    k.undeclared ||= e.undeclared;
    by.set(e.kind, k);
  }
  return [...by.values()].sort((a, b) => Number(b.undeclared) - Number(a.undeclared) || b.n - a.n || (a.kind < b.kind ? -1 : a.kind > b.kind ? 1 : 0));
}

/** `raw_doc_ref 312 · candidate 4 · note 1` */
export function kindCountsText(counts: KindCount[]): string {
  return counts.length ? counts.map((c) => `${c.kind} ${c.n}`).join(" · ") : "none";
}

/** A hash-like value (hex / base64-ish id ≥ 16 chars without spaces): shown shortened. */
export function isHashLike(v: string): boolean {
  return v.length >= 16 && !/\s/.test(v) && /^[0-9a-f]+$/i.test(v.replace(/^sha256:/, ""));
}

/** `0123456789…4567` for a hash; anything shorter than 16 as is. */
export function shortHash(v: string, head = 10, tail = 4): string {
  return v.length > head + tail + 1 ? `${v.slice(0, head)}…${v.slice(-tail)}` : v;
}

/** The last *n* lines (the log is shown collapsed, tail only). */
export function tailLines<T>(lines: T[], n = 50): T[] {
  return lines.length > n ? lines.slice(-n) : lines;
}
