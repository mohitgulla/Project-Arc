/**
 * Ops & pipeline page (E8.7d) helpers: pure functions over the `/api/ops/*` payloads so
 * the page stays declarative and the maths is unit-tested (ops.test.ts).
 */
import type { Schemas } from "./api";
import { formatMoney, formatNumber } from "./format";

export type Session = Schemas["SessionResponse"];
export type Slot = Schemas["Slot"];
export type TimelineRow = Schemas["TimelineRow"];
export type HealthStrip = Schemas["HealthStripResponse"];
export type HealthItem = Schemas["HealthItem"];
export type Alerts = Schemas["AlertsResponse"];
export type Halts = Schemas["HaltsResponse"];
export type RunList = Schemas["RunListResponse"];
export type RunRow = Schemas["RunRow"];
export type RunDetail = Schemas["RunDetailResponse"];
export type StepView = Schemas["StepView"];
export type Budget = Schemas["BudgetResponse"];
export type ContextStore = Schemas["ContextResponse"];
export type ContextEntry = Schemas["ContextEntryResponse"];
export type Sources = Schemas["SourcesResponse"];
export type Llm = Schemas["LlmResponse"];
export type OpsConfig = Schemas["ConfigResponse"];

export type SlotStatus = Slot["status"];

/** Slot colour classes (TOWER_DESIGN tokens): done accent, running pulsing accent, failed
 * neg, skipped / no_change muted, missed warn, future track. */
export const SLOT_CLASS: Record<SlotStatus, string> = {
  done: "bg-accent",
  running: "bg-accent animate-pulse",
  failed: "bg-neg",
  skipped: "bg-muted opacity-60",
  no_change: "bg-muted opacity-60",
  missed: "bg-warn",
  future: "bg-track",
};

export const SLOT_LABEL: Record<SlotStatus, string> = {
  done: "Done",
  running: "Running",
  failed: "Failed",
  skipped: "Skipped",
  no_change: "No change",
  missed: "Missed",
  future: "Scheduled",
};

/** Position of *at* on the [start, end] axis as a 0..100 percentage (clamped). */
export function timelinePct(at: string, start: string, end: string): number {
  const a = Date.parse(at);
  const s = Date.parse(start);
  const e = Date.parse(end);
  if (!(e > s) || Number.isNaN(a)) return 0;
  return Math.max(0, Math.min(100, ((a - s) / (e - s)) * 100));
}

/** Hour ticks (ET wall clock) on the timeline: every *step* hours from start to end. */
export function hourTicks(start: string, end: string, step = 2): Array<{ label: string; pct: number }> {
  const s = Date.parse(start);
  const e = Date.parse(end);
  const out: Array<{ label: string; pct: number }> = [];
  const startHour = Number(start.slice(11, 13));
  for (let t = s, h = startHour; t <= e; t += step * 3_600_000, h += step) {
    out.push({ label: `${String(h % 24).padStart(2, "0")}:00`, pct: ((t - s) / (e - s)) * 100 });
  }
  return out;
}

/** One-line hover text for a slot. */
export function slotTitle(slot: Slot): string {
  const time = slot.at.slice(11, 16);
  const head = `${slot.job} ${time} ET · ${SLOT_LABEL[slot.status]}`;
  const run = slot.run;
  if (!run) return head;
  const detail = run.error ?? run.summary;
  const dur = run.duration_ms != null ? ` · ${formatDuration(run.duration_ms)}` : "";
  const steps = slot.chain_steps ? ` · ${slot.chain_steps} chain steps` : "";
  return `${head}${dur}${steps}${detail ? ` — ${detail}` : ""}`;
}

/** `850 ms`, `42 s`, `3 min 5 s`, `1 h 2 min`. */
export function formatDuration(ms: number | null | undefined): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms} ms`;
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return s % 60 ? `${m} min ${s % 60} s` : `${m} min`;
  const h = Math.floor(m / 60);
  return m % 60 ? `${h} h ${m % 60} min` : `${h} h`;
}

/** Seconds -> duration text (alerts, halts). */
export function formatSeconds(s: number | null | undefined): string {
  return s == null ? "—" : formatDuration(s * 1000);
}

/** Counts per status for the legend, in display order, zero counts dropped. */
export function slotCounts(counts: Record<string, number>): Array<{ status: SlotStatus; n: number }> {
  return (Object.keys(SLOT_LABEL) as SlotStatus[])
    .map((status) => ({ status, n: counts[status] ?? 0 }))
    .filter((x) => x.n > 0);
}

/** Loop runs: full vs no_change today (the D31 two-speed evidence). */
export function loopSplit(row: TimelineRow | null | undefined): { full: number; noChange: number; failed: number } {
  const slots = row?.slots ?? [];
  return {
    full: slots.filter((s) => s.status === "done").length,
    noChange: slots.filter((s) => s.status === "no_change").length,
    failed: slots.filter((s) => s.status === "failed").length,
  };
}

// -- day control ---------------------------------------------------------------------

export const DAY_PRESETS = [
  { value: "today", label: "Today" },
  { value: "yesterday", label: "Yesterday" },
] as const;

/** `?day=` -> the API value: today (default), yesterday, or a YYYY-MM-DD pick. */
export function dayParam(v: string | null): string {
  if (!v || v === "today") return "today";
  if (v === "yesterday") return v;
  return /^\d{4}-\d{2}-\d{2}$/.test(v) ? v : "today";
}

// -- runs filters --------------------------------------------------------------------

export interface RunQuery {
  job: string[];
  status: string[];
  day: string | null;
  chain: string | null;
  page: number;
}

export function runQuery(params: URLSearchParams): RunQuery {
  const list = (k: string) => (params.get(k) ?? "").split(",").filter(Boolean);
  const page = Number(params.get("page") ?? "1");
  return {
    job: list("job"),
    status: list("status"),
    day: params.get("rday"),
    chain: params.get("chain"),
    page: Number.isInteger(page) && page > 0 ? page : 1,
  };
}

/** The `/api/ops/runs` query for a RunQuery (empty filters dropped). */
export function runApiQuery(q: RunQuery, size = 50): Record<string, string | number> {
  const out: Record<string, string | number> = { page: q.page, size };
  if (q.job.length) out.job = q.job.join(",");
  if (q.status.length) out.status = q.status.join(",");
  if (q.day) out.day = q.day;
  if (q.chain) out.chain = q.chain;
  return out;
}

export function runStatusTone(r: Pick<RunRow, "status" | "no_change">): "pos" | "neg" | "neutral" | "warn" {
  if (r.status === "failed") return "neg";
  if (r.status === "running") return "warn";
  if (r.status === "ok" && !r.no_change) return "pos";
  return "neutral";
}

export function runStatusLabel(r: Pick<RunRow, "status" | "no_change">): string {
  if (r.status === "ok" && r.no_change) return "no change";
  return r.status;
}

// -- run detail: manifest groups -----------------------------------------------------

type Manifest = Record<string, unknown>;

export interface ManifestGroup {
  title: string;
  rows: Array<{ label: string; value: string }>;
}

function text(v: unknown): string {
  if (v === null || v === undefined || v === "") return "—";
  if (Array.isArray(v)) return v.length ? v.map(text).join(", ") : "—";
  if (typeof v === "object") {
    const entries = Object.entries(v as Record<string, unknown>);
    return entries.length ? entries.map(([k, x]) => `${k}: ${text(x)}`).join(" · ") : "—";
  }
  if (typeof v === "boolean") return v ? "yes" : "no";
  return String(v);
}

const GROUPS: Array<{ title: string; keys: Array<[string, string]> }> = [
  {
    title: "Identity & trigger",
    keys: [
      ["run_id", "Run id"],
      ["job", "Job"],
      ["job_kind", "Kind"],
      ["chain_run_id", "Chain id"],
      ["step_index", "Step"],
      ["attempt", "Attempt"],
      ["reason", "Trigger"],
      ["event_id", "Event"],
      ["correlation", "Correlation"],
    ],
  },
  {
    title: "Timing & session",
    keys: [
      ["scheduled_for", "Scheduled"],
      ["tick_now", "Tick"],
      ["started_at", "Started"],
      ["finished_at", "Finished"],
      ["duration_ms", "Duration"],
      ["market_session", "Session"],
      ["trading_day", "Trading day"],
    ],
  },
  {
    title: "Outcome",
    keys: [
      ["status", "Status"],
      ["summary", "Summary"],
      ["error_class", "Error class"],
      ["error", "Error"],
      ["metrics", "Metrics"],
    ],
  },
  {
    title: "Environment",
    keys: [
      ["arc_env", "Env"],
      ["account_profile", "Account profile"],
      ["halted", "Halted"],
      ["auto_approve", "Auto-approve"],
      ["order_budget", "Order budget"],
      ["env_flags", "Env flags"],
    ],
  },
  {
    title: "Code & config",
    keys: [
      ["git_sha", "Git sha"],
      ["git_dirty", "Dirty tree"],
      ["arc_version", "Arc version"],
      ["python", "Python"],
      ["host", "Host"],
      ["config_version", "Config version"],
      ["config_hashes", "Config hashes"],
    ],
  },
  {
    title: "Inputs",
    keys: [
      ["snapshot_ids", "Snapshots"],
      ["input_digest", "Input digest"],
      ["input_counts", "Input counts"],
    ],
  },
  {
    title: "LLM",
    keys: [
      ["models_requested", "Models requested"],
      ["models_served", "Models served"],
      ["input_tokens", "Input tokens"],
      ["output_tokens", "Output tokens"],
      ["llm_latency_ms", "Latency"],
      ["cost_usd", "Cost"],
    ],
  },
];

/** The manifest rendered as KeyValueList groups in the manifest's own order. */
export function manifestGroups(m: Manifest | null | undefined): ManifestGroup[] {
  if (!m) return [];
  return GROUPS.map((g) => ({
    title: g.title,
    rows: g.keys
      .filter(([k]) => k in m)
      .map(([k, label]) => {
        const v = m[k];
        let value = text(v);
        if ((k === "duration_ms" || k === "llm_latency_ms") && typeof v === "number") value = formatDuration(v);
        if (k === "cost_usd" && typeof v === "number") value = formatMoney(v, "price");
        if ((k === "input_tokens" || k === "output_tokens") && typeof v === "number") value = formatNumber(v);
        if (k === "git_sha" && typeof v === "string") value = v.slice(0, 12);
        return { label, value };
      }),
  })).filter((g) => g.rows.length > 0);
}

/** External inputs from the manifest: name, source, as_of, digest. */
export function externalInputs(m: Manifest | null | undefined): Array<{ name: string; source: string; asOf: string; digest: string; count: string }> {
  const ext = (m?.external_inputs ?? []) as Array<Record<string, unknown>>;
  return ext.map((e) => ({
    name: text(e.name),
    source: text(e.source),
    asOf: text(e.as_of),
    digest: typeof e.digest === "string" ? e.digest.slice(0, 12) : "—",
    count: text(e.count),
  }));
}

/** Declared vs actual: one row per kind in either set, flagged when undeclared. */
export function contractRows(
  declared: string[] | null | undefined,
  actual: string[],
  undeclared: string[],
): Array<{ kind: string; declared: boolean; used: boolean; mismatch: boolean }> {
  const kinds = [...new Set([...(declared ?? []), ...actual])].sort();
  const bad = new Set(undeclared);
  const used = new Set(actual);
  const dec = new Set(declared ?? []);
  return kinds.map((kind) => ({ kind, declared: dec.has(kind), used: used.has(kind), mismatch: bad.has(kind) }));
}

export const LOG_LEVELS = ["debug", "info", "warning", "error"] as const;

/** Log lines at or above *min* level. */
export function filterLog<T extends { level: string }>(lines: T[], min: string): T[] {
  const rank = (l: string) => {
    const i = LOG_LEVELS.indexOf(l.toLowerCase() as (typeof LOG_LEVELS)[number]);
    return i < 0 ? 1 : i;
  };
  const floor = rank(min);
  return lines.filter((l) => rank(l.level) >= floor);
}

// -- budget --------------------------------------------------------------------------

/** Tier marks on the 0..limit budget bar, as 0..1 fractions. */
export function budgetMarks(b: Pick<Budget, "limit" | "restrict_at" | "open_limit">): Array<{ label: string; at: number }> {
  if (!b.limit) return [];
  return [
    { label: `restrict ${b.restrict_at}`, at: b.restrict_at / b.limit },
    { label: `opens stop ${b.open_limit}`, at: b.open_limit / b.limit },
  ];
}

// -- context -------------------------------------------------------------------------

/** Time left until *iso* (from *now*), compact. */
export function timeLeft(iso: string | null | undefined, now: number): string {
  if (!iso) return "no expiry";
  const s = Math.round((Date.parse(iso) - now) / 1000);
  if (s <= 0) return "expired";
  return `${formatSeconds(s)} left`;
}

// -- LLM -----------------------------------------------------------------------------

/** StackedBars rows (cost by persona per day) and the series list. */
export function llmBars(llm: Llm): {
  data: Array<Record<string, number | string>>;
  series: Array<{ key: string; label: string; color: string }>;
} {
  const personas = llm.personas;
  const series = personas.map((p, i) => ({ key: p, label: personaLabel(p), color: `var(--series-${(i % 5) + 1})` }));
  const data = llm.series.map((d) => {
    const row: Record<string, number | string> = { label: d.day.slice(5) };
    for (const p of personas) row[p] = d.by_persona[p] ?? 0;
    return row;
  });
  return { data, series };
}

export function personaLabel(p: string): string {
  const [head = p, tail] = p.split(".");
  const cap = head.charAt(0).toUpperCase() + head.slice(1);
  return tail ? `${cap} (${tail})` : cap;
}

/** `$1.13` hero text for LLM cost (cents kept: these are small numbers). */
export function llmCost(v: number): string {
  return formatMoney(v, "price");
}

// -- config --------------------------------------------------------------------------

/** Config keys grouped by registry group, overridden keys first within a group. */
export function configGroups(keys: OpsConfig["keys"]): Array<{ group: string; keys: OpsConfig["keys"] }> {
  const by = new Map<string, OpsConfig["keys"]>();
  for (const k of keys) by.set(k.group, [...(by.get(k.group) ?? []), k]);
  return [...by.entries()].map(([group, ks]) => ({
    group,
    keys: [...ks].sort((a, b) => Number(b.source === "override") - Number(a.source === "override")),
  }));
}
