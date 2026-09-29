import { moneyParts, type MoneyKind } from "../lib/format";

/** Money with the `$` glyph rendered smaller and raised (TOWER_DESIGN §3). */
export function Money({
  value,
  kind = "pnl",
  explicitSign = false,
  className = "",
}: {
  value: number;
  kind?: MoneyKind;
  explicitSign?: boolean;
  className?: string;
}) {
  const p = moneyParts(value, kind, { explicitSign });
  return (
    <span className={`whitespace-nowrap tabular-nums ${className}`}>
      {p.sign}
      <span className="arc-money-glyph">{p.glyph}</span>
      {p.number}
    </span>
  );
}
