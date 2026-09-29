import { useId } from "react";
import { Area, AreaChart, ReferenceLine, ResponsiveContainer, Tooltip, YAxis } from "recharts";

import { formatEt, formatMoney, type MoneyKind } from "../lib/format";
import { TooltipBox } from "./chartKit";

export interface TrendPoint {
  t: string; // ISO time
  v: number;
}

/**
 * Axis-less line, series colour by the sign of the change over the range, gradient fill
 * (~30% -> 0), hollow-ring endpoint, hover tooltip (time + value), optional dotted
 * reference line (prev close / start of range). TOWER_DESIGN §4.
 */
export function TrendChart({
  data,
  reference,
  height = 180,
  kind = "equity",
  format,
}: {
  data: TrendPoint[];
  reference?: number;
  height?: number;
  kind?: MoneyKind;
  format?: (v: number) => string;
}) {
  const gid = `trend-${useId().replace(/:/g, "")}`;
  const first = data[0]?.v ?? 0;
  const last = data[data.length - 1]?.v ?? 0;
  const color = last >= (reference ?? first) ? "var(--pos)" : "var(--neg)";
  const fmt = format ?? ((v: number) => formatMoney(v, kind));
  const lastIdx = data.length - 1;

  return (
    <div style={{ height }} className="w-full" data-testid="trend-chart">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 8, right: 8, bottom: 4, left: 8 }}>
          <defs>
            <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity={0.3} />
              <stop offset="100%" stopColor={color} stopOpacity={0} />
            </linearGradient>
          </defs>
          <YAxis hide domain={["dataMin", "dataMax"]} />
          {reference !== undefined && (
            <ReferenceLine y={reference} stroke="var(--text-muted)" strokeDasharray="2 4" strokeWidth={1} />
          )}
          <Tooltip
            cursor={{ stroke: "var(--border-input)", strokeWidth: 1 }}
            content={({ active, payload }) => {
              const p = payload?.[0]?.payload as TrendPoint | undefined;
              if (!active || !p) return null;
              return <TooltipBox title={formatEt(p.t)}>{fmt(p.v)}</TooltipBox>;
            }}
          />
          <Area
            type="monotone"
            dataKey="v"
            stroke={color}
            strokeWidth={2.5}
            fill={`url(#${gid})`}
            isAnimationActive={false}
            dot={(props: { cx?: number; cy?: number; index?: number }) =>
              props.index === lastIdx && props.cx !== undefined && props.cy !== undefined ? (
                <circle
                  key="end"
                  cx={props.cx}
                  cy={props.cy}
                  r={4.5}
                  fill="var(--bg-card)"
                  stroke={color}
                  strokeWidth={2.5}
                />
              ) : (
                <g key={`d${props.index}`} />
              )
            }
            activeDot={{ r: 4, fill: color, stroke: "var(--bg-card)", strokeWidth: 2 }}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
