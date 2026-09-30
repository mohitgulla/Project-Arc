import { useId } from "react";
import { Area, AreaChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { formatAxisMoney, formatMoney, formatNumber } from "../lib/format";
import { TooltipBox } from "./chartKit";

export interface PayoffPoint {
  spot: number;
  pnl: number;
}

/**
 * At-expiry payoff (P&L vs underlying): pos fill above zero, neg below, dotted zero line,
 * dashed markers for breakevens, the entry spot and the latest spot (TOWER_DESIGN §4).
 */
export function PayoffChart({
  points,
  breakevens = [],
  entrySpot,
  latestSpot,
  height = 200,
}: {
  points: PayoffPoint[];
  breakevens?: number[];
  entrySpot?: number | null;
  latestSpot?: number | null;
  height?: number;
}) {
  const ys = points.map((p) => p.pnl);
  const max = Math.max(0, ...ys);
  const min = Math.min(0, ...ys);
  // Split the fill at y=0: gradient offset where zero sits between max and min.
  const zero = max === min ? 0.5 : max / (max - min);
  const gid = `payoff-${useId().replace(/:/g, "")}`;
  return (
    <div style={{ height }} className="w-full" data-testid="payoff-chart">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={points} margin={{ top: 8, right: 12, bottom: 4, left: 4 }}>
          <defs>
            <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
              <stop offset={0} stopColor="var(--pos)" stopOpacity={0.35} />
              <stop offset={zero} stopColor="var(--pos)" stopOpacity={0.05} />
              <stop offset={zero} stopColor="var(--neg)" stopOpacity={0.05} />
              <stop offset={1} stopColor="var(--neg)" stopOpacity={0.35} />
            </linearGradient>
            <linearGradient id={`${gid}-line`} x1="0" y1="0" x2="0" y2="1">
              <stop offset={zero} stopColor="var(--pos)" />
              <stop offset={zero} stopColor="var(--neg)" />
            </linearGradient>
          </defs>
          <XAxis
            dataKey="spot"
            type="number"
            domain={["dataMin", "dataMax"]}
            tick={{ fill: "var(--text-muted)", fontSize: 11 }}
            tickFormatter={(v: number) => formatNumber(v, 0)}
            stroke="var(--border)"
          />
          <YAxis
            width={48}
            tick={{ fill: "var(--text-muted)", fontSize: 11 }}
            tickFormatter={(v: number) => formatAxisMoney(v)}
            stroke="var(--border)"
          />
          <ReferenceLine y={0} stroke="var(--text-muted)" strokeDasharray="2 4" />
          {breakevens.map((b) => (
            <ReferenceLine key={`be${b}`} x={b} stroke="var(--text-muted)" strokeDasharray="4 4" />
          ))}
          {entrySpot != null && (
            <ReferenceLine x={entrySpot} stroke="var(--accent)" strokeDasharray="1 3" label={{ value: "entry", position: "top", fill: "var(--text-muted)", fontSize: 10 }} />
          )}
          {latestSpot != null && (
            <ReferenceLine x={latestSpot} stroke="var(--accent)" label={{ value: "now", position: "top", fill: "var(--text-muted)", fontSize: 10 }} />
          )}
          <Tooltip
            cursor={{ stroke: "var(--border-input)", strokeWidth: 1 }}
            content={({ active, payload }) => {
              const p = payload?.[0]?.payload as PayoffPoint | undefined;
              if (!active || !p) return null;
              return <TooltipBox title={`at ${formatNumber(p.spot, 2)}`}>{formatMoney(p.pnl, "pnl")}</TooltipBox>;
            }}
          />
          <Area
            type="linear"
            dataKey="pnl"
            stroke={`url(#${gid}-line)`}
            strokeWidth={2}
            fill={`url(#${gid})`}
            isAnimationActive={false}
            baseValue={0}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
