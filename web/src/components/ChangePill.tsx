import { changePill, type Metric, type Polarity, type Tone } from "../lib/format";

const TONE: Record<Tone, string> = {
  pos: "bg-pos-bg text-pos-text",
  neg: "bg-neg-bg text-neg-text",
  neutral: "bg-control text-secondary",
};

/**
 * `↗ 148.1%`: glyph + unsigned value, colour = favourability for *metric* (§3 rule table).
 * *value* is the signed change (a fraction by default; pass *format* for money/counts).
 */
export function ChangePill({
  value,
  metric,
  format,
  className = "",
}: {
  value: number;
  metric: Metric | Polarity;
  format?: (abs: number) => string;
  className?: string;
}) {
  const c = changePill(value, metric, format);
  return (
    <span
      data-tone={c.tone}
      className={`relative inline-flex items-center gap-1 rounded-pill px-1.5 py-0.5 text-caption font-semibold tabular-nums ${TONE[c.tone]} ${className}`}
    >
      {c.glyph && <span aria-hidden="true">{c.glyph}</span>}
      <span className="sr-only">{c.direction === "up" ? "up" : c.direction === "down" ? "down" : "unchanged"}</span>
      {c.text}
    </span>
  );
}
