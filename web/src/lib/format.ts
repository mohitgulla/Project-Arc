/**
 * Number and time formatting: docs/TOWER_DESIGN.md §3. Pure functions, unit-tested in
 * format.test.ts; every rule in §3 has a test. Components never format numbers inline.
 */

export const ET_ZONE = "America/New_York";

// ---------------------------------------------------------------------------
// Money
// ---------------------------------------------------------------------------

/** What a money value is: decides cents vs whole dollars (§3). */
export type MoneyKind =
  | "price"
  | "fill"
  | "pnl"
  | "equity"
  | "max_loss"
  | "allocation"
  | "buying_power";

const CENTS_KINDS: ReadonlySet<MoneyKind> = new Set(["price", "fill", "pnl"]);

export interface MoneyParts {
  /** "-" for negative values, "+" only when an explicit sign was requested, else "". */
  sign: "" | "-" | "+";
  /** Rendered smaller and raised by <Money> (.arc-money-glyph). */
  glyph: "$";
  /** Digits with thousands commas, e.g. "7,260.02". */
  number: string;
}

/** Cents on prices, fills and P&L; whole dollars on equity, max loss, allocation (§3). */
export function moneyDecimals(kind: MoneyKind): 0 | 2 {
  return CENTS_KINDS.has(kind) ? 2 : 0;
}

function groupDigits(abs: number, decimals: number): string {
  return abs.toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
    useGrouping: true,
  });
}

/** Round half away from zero at *decimals* (avoids "-0.00" and float drift). */
function roundTo(value: number, decimals: number): number {
  const f = 10 ** decimals;
  return (Math.sign(value) * Math.round(Math.abs(value) * f + Number.EPSILON)) / f;
}

export function moneyParts(
  value: number,
  kind: MoneyKind = "pnl",
  opts: { explicitSign?: boolean } = {},
): MoneyParts {
  const decimals = moneyDecimals(kind);
  const rounded = roundTo(value, decimals);
  const sign: MoneyParts["sign"] =
    rounded < 0 ? "-" : opts.explicitSign && rounded > 0 ? "+" : "";
  return { sign, glyph: "$", number: groupDigits(Math.abs(rounded), decimals) };
}

/** Plain-text money: `-$7,260.02` (leading minus, never parentheses). */
export function formatMoney(
  value: number,
  kind: MoneyKind = "pnl",
  opts: { explicitSign?: boolean } = {},
): string {
  const p = moneyParts(value, kind, opts);
  return `${p.sign}${p.glyph}${p.number}`;
}

/** Axis ticks: `$71K`, `-$9K`, `$1.2M`, `$950`. One decimal at most, trailing .0 dropped. */
export function formatAxisMoney(value: number): string {
  const abs = Math.abs(value);
  const sign = value < 0 && abs >= 0.5 ? "-" : "";
  const compact = (n: number, suffix: string) => {
    const digits = n >= 10 ? 0 : 1;
    const text = roundTo(n, digits).toFixed(digits).replace(/\.0$/, "");
    return `${text}${suffix}`;
  };
  let body: string;
  if (abs >= 1e9) body = compact(abs / 1e9, "B");
  else if (abs >= 1e6) body = compact(abs / 1e6, "M");
  else if (abs >= 1e3) body = compact(abs / 1e3, "K");
  else body = String(Math.round(abs));
  // 999_950 rounds to "1000K": promote to the next unit.
  if (body === "1000K") body = "1M";
  if (body === "1000M") body = "1B";
  return `${sign}$${body}`;
}

// ---------------------------------------------------------------------------
// Percent
// ---------------------------------------------------------------------------

/**
 * Percent from a **fraction** (0.0125 -> "1.25%"): 2 decimals below 100 %, 1 decimal at
 * or above (`148.1%`). Sign only when *explicitSign* or negative.
 */
export function formatPercent(fraction: number, opts: { explicitSign?: boolean } = {}): string {
  const pct = fraction * 100;
  const abs = Math.abs(pct);
  let decimals = abs >= 100 ? 1 : 2;
  let rounded = roundTo(abs, decimals);
  if (decimals === 2 && rounded >= 100) {
    decimals = 1;
    rounded = roundTo(abs, 1);
  }
  const text = rounded.toFixed(decimals);
  const negative = pct < 0 && rounded > 0;
  const sign = negative ? "-" : opts.explicitSign && rounded > 0 ? "+" : "";
  return `${sign}${text}%`;
}

// ---------------------------------------------------------------------------
// Change pill: glyph + unsigned value; colour = favourability (§3 rule table)
// ---------------------------------------------------------------------------

/** Higher is better (green up), lower is better (red up), or neutral (no colour). */
export type Polarity = "up_good" | "up_bad" | "neutral";

/** §3 rule table, by metric name. Unlisted metrics must pass a Polarity explicitly. */
export const METRIC_POLARITY = {
  pnl: "up_good",
  equity: "up_good",
  pop: "up_good",
  net_ev: "up_good",
  win_rate: "up_good",
  cost: "up_bad",
  slippage: "up_bad",
  spend: "up_bad",
  drawdown: "up_bad",
  max_loss: "up_bad",
  gate_violations: "up_bad",
  latency: "up_bad",
  llm_cost: "up_bad",
  contracts: "neutral",
  count: "neutral",
  delta_exposure: "neutral",
} as const satisfies Record<string, Polarity>;

export type Metric = keyof typeof METRIC_POLARITY;
export type Tone = "pos" | "neg" | "neutral";
export type Direction = "up" | "down" | "flat";

export interface ChangeView {
  direction: Direction;
  /** "↗" up, "↘" down, "" flat. */
  glyph: "↗" | "↘" | "";
  /** Unsigned value text: the glyph carries direction (§3 "no sign"). */
  text: string;
  tone: Tone;
}

export function polarityOf(metric: Metric | Polarity): Polarity {
  return metric in METRIC_POLARITY ? METRIC_POLARITY[metric as Metric] : (metric as Polarity);
}

export function toneFor(direction: Direction, polarity: Polarity): Tone {
  if (direction === "flat" || polarity === "neutral") return "neutral";
  const good = (direction === "up") === (polarity === "up_good");
  return good ? "pos" : "neg";
}

/**
 * A change pill. *value* is the signed change; *format* renders its absolute value
 * (percent from a fraction by default). `changePill(1.481, "pnl")` -> ↗ 148.1% green.
 */
export function changePill(
  value: number,
  metric: Metric | Polarity,
  format: (abs: number) => string = (abs) => formatPercent(abs),
): ChangeView {
  const text = format(Math.abs(value));
  const zeroText = format(0);
  const direction: Direction = value === 0 || text === zeroText ? "flat" : value > 0 ? "up" : "down";
  const glyph = direction === "up" ? "↗" : direction === "down" ? "↘" : "";
  return { direction, glyph, text, tone: toneFor(direction, polarityOf(metric)) };
}

/** Detail-panel total return: explicit sign, parenthesised percent, neutral colour. */
export function formatTotalReturn(amount: number, fraction: number): string {
  return `${formatMoney(amount, "pnl", { explicitSign: true })} (${formatPercent(fraction, {
    explicitSign: true,
  })})`;
}

// ---------------------------------------------------------------------------
// Times (always ET) and ages
// ---------------------------------------------------------------------------

const ET_PARTS = new Intl.DateTimeFormat("en-US", {
  timeZone: ET_ZONE,
  weekday: "short",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

function toDate(t: Date | string | number): Date {
  return t instanceof Date ? t : new Date(t);
}

/** `Mon 09-28 15:35` in ET, whatever the browser's zone. */
export function formatEt(t: Date | string | number): string {
  const d = toDate(t);
  if (Number.isNaN(d.getTime())) return "—";
  const parts = Object.fromEntries(ET_PARTS.formatToParts(d).map((p) => [p.type, p.value]));
  return `${parts.weekday} ${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
}

/** `just now`, `4m ago`, `2h ago`, `3d ago` (floor; future times read `just now`). */
export function formatAge(t: Date | string | number, now: Date | number = Date.now()): string {
  const secs = ageSeconds(t, now);
  if (secs === null) return "—";
  if (secs < 60) return "just now";
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

export function ageSeconds(t: Date | string | number, now: Date | number = Date.now()): number | null {
  const d = toDate(t);
  if (Number.isNaN(d.getTime())) return null;
  const ref = typeof now === "number" ? now : now.getTime();
  return Math.max(0, (ref - d.getTime()) / 1000);
}

/** Stale when the age exceeds 3x the producing job's cadence (§3; cadence from /api/meta). */
export const STALE_FACTOR = 3;

export function isStale(
  t: Date | string | number | null | undefined,
  cadenceSeconds: number,
  now: Date | number = Date.now(),
): boolean {
  if (t === null || t === undefined) return true;
  const secs = ageSeconds(t, now);
  return secs === null || secs > STALE_FACTOR * cadenceSeconds;
}

/** `as of Mon 09-28 15:35 · 4m ago`, or `stale · as of …` past the threshold. */
export function formatAsOf(
  t: Date | string | number,
  now: Date | number = Date.now(),
  cadenceSeconds?: number,
): string {
  const base = `as of ${formatEt(t)} · ${formatAge(t, now)}`;
  return cadenceSeconds !== undefined && isStale(t, cadenceSeconds, now) ? `stale · ${base}` : base;
}

// ---------------------------------------------------------------------------
// Options legs
// ---------------------------------------------------------------------------

export interface OccLeg {
  root: string;
  expiration: string; // YYYY-MM-DD
  right: "C" | "P";
  strike: number;
}

const OCC = /^([A-Z0-9.]{1,6})\s*(\d{2})(\d{2})(\d{2})([CP])(\d{8})$/;

/** Parse an OCC symbol (`AMD261002C00620000`); null when it is not one. */
export function parseOcc(symbol: string): OccLeg | null {
  const m = OCC.exec(symbol.trim().toUpperCase());
  if (!m) return null;
  const [, root, yy, mm, dd, right, strike] = m as unknown as [
    string,
    string,
    string,
    string,
    string,
    "C" | "P",
    string,
  ];
  return {
    root,
    expiration: `20${yy}-${mm}-${dd}`,
    right,
    strike: Number(strike) / 1000,
  };
}

/** `AMD 10/02 620C` (root, M/DD, strike, C/P); falls back to the raw symbol. */
export function formatLeg(symbol: string): string {
  const leg = parseOcc(symbol);
  if (!leg) return symbol;
  const [, mm, dd] = leg.expiration.split("-");
  const strike = Number.isInteger(leg.strike)
    ? String(leg.strike)
    : leg.strike.toFixed(3).replace(/0+$/, "").replace(/\.$/, "");
  return `${leg.root} ${mm}/${dd} ${strike}${leg.right}`;
}

// ---------------------------------------------------------------------------
// Plain numbers
// ---------------------------------------------------------------------------

/** Neutral quantities (contracts, counts, Greeks): commas, up to *decimals* places. */
export function formatNumber(value: number, decimals = 0): string {
  return roundTo(value, decimals).toLocaleString("en-US", {
    minimumFractionDigits: 0,
    maximumFractionDigits: decimals,
  });
}
