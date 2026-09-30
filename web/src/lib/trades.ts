/**
 * Trades page model (E8.7b): URL <-> API query, stage labels and the lifecycle stepper
 * mapping. Pure functions, unit-tested in trades.test.ts.
 */
import type { Stage as LifecycleStage } from "../components/StatusStepper";
import type { Schemas } from "./api";

export type TradeRow = Schemas["TradeRow"];
export type TradeList = Schemas["TradeListResponse"];
export type TradeSummary = Schemas["TradeSummary"];
export type TradeFilterOptions = Schemas["TradeFilterOptions"];
export type TradeDetail = Schemas["TradeDetail"];
export type DecisionItem = Schemas["DecisionItem"];
export type SearchResponse = Schemas["SearchResponse"];
export type TradeStage = TradeRow["stage"];

/** Multi-value filters: comma lists in the URL (`?stage=open,closed`). */
export const MULTI_KEYS = [
  "ticker",
  "kind",
  "structure",
  "stage",
  "exit_reason",
  "reason_code",
  "account_profile",
] as const;
/** Single-value filters. */
export const SINGLE_KEYS = ["date", "date_from", "date_to", "min_net_ev", "min_pop", "q"] as const;
/** Every URL key a filter owns (Clear removes these; any change resets the page). */
export const FILTER_KEYS = [...MULTI_KEYS, ...SINGLE_KEYS] as const;

export const SORT_KEYS = [
  "time",
  "ticker",
  "contracts",
  "limit",
  "net_ev",
  "pop",
  "slippage_bps",
  "realized_pnl",
] as const;
export type SortKey = (typeof SORT_KEYS)[number];
export type SortDir = "asc" | "desc";

export const PAGE_SIZES = [25, 50, 100, 200] as const;
export const DEFAULT_SIZE = 50;

export interface TradeQuery {
  query: Record<string, string>;
  page: number;
  size: number;
  sort: SortKey;
  dir: SortDir;
}

function int(raw: string | null, fallback: number, min: number, max: number): number {
  const n = raw === null ? NaN : Number(raw);
  return Number.isInteger(n) && n >= min && n <= max ? n : fallback;
}

/** The API query for the current URL; unknown keys and bad values fall back to defaults. */
export function tradeQuery(params: URLSearchParams): TradeQuery {
  const query: Record<string, string> = {};
  for (const k of FILTER_KEYS) {
    const v = params.get(k)?.trim();
    if (v) query[k] = v;
  }
  // Overview's "VIEW ALL" link says `since=today`.
  if (!query.date && params.get("since") === "today") query.date = "today";
  if (query.date !== "custom") {
    delete query.date_from;
    delete query.date_to;
  }
  const sortRaw = params.get("sort") ?? "time";
  const sort = (SORT_KEYS as readonly string[]).includes(sortRaw) ? (sortRaw as SortKey) : "time";
  const dir: SortDir = params.get("dir") === "asc" ? "asc" : "desc";
  const size = int(params.get("size"), DEFAULT_SIZE, 1, 200);
  const page = int(params.get("page"), 1, 1, 1_000_000);
  return { query, page, size, sort, dir };
}

/** API query object (filters + paging + sort) for `apiGet("/api/trades")`. */
export function apiQuery(q: TradeQuery): Record<string, string | number> {
  return { ...q.query, page: q.page, size: q.size, sort: q.sort, dir: q.dir };
}

/** Number of pages (at least 1) for *total* rows. */
export function pageCount(total: number, size: number): number {
  return Math.max(1, Math.ceil(total / size));
}

/** Count of active filter values (for the mobile "Filters (n)" button). */
export function activeFilterCount(params: URLSearchParams): number {
  const { query } = tradeQuery(params);
  let n = 0;
  for (const [k, v] of Object.entries(query)) {
    if (k === "date_from" || k === "date_to") continue;
    n += (MULTI_KEYS as readonly string[]).includes(k) ? v.split(",").filter(Boolean).length : 1;
  }
  return n;
}

// ---------------------------------------------------------------------------
// Labels
// ---------------------------------------------------------------------------

export const STAGE_LABEL: Record<TradeStage, string> = {
  proposed: "Proposed",
  gate_pass: "Gate passed",
  gate_fail: "Gate failed",
  approved: "Approved",
  rejected: "Rejected",
  expired: "Expired",
  filled: "Filled",
  cancelled: "Not filled",
  open: "Open",
  closed: "Closed",
};

export const DATE_LABEL: Record<string, string> = {
  today: "Today",
  "7d": "7 days",
  "30d": "30 days",
  mtd: "MTD",
  ytd: "YTD",
  all: "All",
  custom: "Custom",
};

export const EXIT_REASON_LABEL: Record<string, string> = {
  profit_target: "Profit target",
  take_profit: "Take profit",
  stop: "Stop",
  stop_loss: "Stop loss",
  dte_exit: "DTE exit",
  expiry: "Expiry",
  reallocate: "Reallocate",
  manual: "Manual",
  owner: "Owner",
};

/** `profit_target` -> `Profit target` (known labels first, else humanised). */
export function humanize(code: string | null | undefined): string {
  if (!code) return "—";
  if (EXIT_REASON_LABEL[code]) return EXIT_REASON_LABEL[code];
  const s = code.replace(/[_:]+/g, " ").trim();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/** Row stage -> the lifecycle stepper (reached / failed stage). */
export function stepperFor(stage: TradeStage): { reached: LifecycleStage; failedAt?: LifecycleStage } {
  switch (stage) {
    case "proposed":
      return { reached: "proposed" };
    case "gate_pass":
      return { reached: "gate" };
    case "gate_fail":
      return { reached: "gate", failedAt: "gate" };
    case "approved":
      return { reached: "approval" };
    case "rejected":
    case "expired":
      return { reached: "approval", failedAt: "approval" };
    case "cancelled":
      return { reached: "execution", failedAt: "execution" };
    case "filled":
      return { reached: "filled" };
    case "open":
      return { reached: "open" };
    case "closed":
      return { reached: "closed" };
  }
}

/** Stage tone for the text label. */
export function stageTone(stage: TradeStage): "pos" | "neg" | "neutral" {
  if (stage === "gate_fail" || stage === "rejected" || stage === "expired" || stage === "cancelled")
    return "neg";
  if (stage === "filled" || stage === "open" || stage === "closed") return "pos";
  return "neutral";
}

/** Short hash for display: first 10 hex chars. */
export const shortHash = (h: string) => h.slice(0, 10);
