import { useId } from "react";
import {
  Area,
  CartesianGrid,
  ComposedChart,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
  ReferenceLine,
} from "recharts";

import { formatAxisMoney, formatMoney } from "../lib/format";
import { TooltipBox } from "./chartKit";

export interface EquityPointView {
  label: string; // "Sep 2"
  equity: number;
  /** equity − running peak (≤ 0): the drawdown band under the line. */
  underwater: number;
}

/**
 * Equity line with the drawdown shaded beneath it (§4 TrendChart styling, plus a red
 * band = distance below the running peak on its own lower strip).
 */
export function EquityDrawdownChart({ data, height = 200 }: { data: EquityPointView[]; height?: number }) {
  const gid = `eq-${useId().replace(/:/g, "")}`;
  const first = data[0]?.equity ?? 0;
  const last = data[data.length - 1]?.equity ?? 0;
  const color = last >= first ? "var(--pos)" : "var(--neg)";
  const minDd = Math.min(0, ...data.map((d) => d.underwater));
  return (
    <div style={{ height }} className="w-full" data-testid="equity-drawdown-chart">
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 8 }}>
          <defs>
            <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity={0.25} />
              <stop offset="100%" stopColor={color} stopOpacity={0} />
            </linearGradient>
          </defs>
          <XAxis
            dataKey="label"
            tickLine={false}
            axisLine={false}
            interval="preserveStartEnd"
            tick={{ fill: "var(--text-muted)", fontSize: 11 }}
          />
          <YAxis yAxisId="eq" hide domain={["dataMin", "dataMax"]} />
          {/* The drawdown strip: 0 at the top of the lower third, the worst dip at the bottom. */}
          <YAxis yAxisId="dd" hide domain={[minDd < 0 ? minDd : -1, minDd < 0 ? -minDd * 2 : 2]} />
          <Tooltip
            cursor={{ stroke: "var(--border-input)", strokeWidth: 1 }}
            content={({ active, payload }) => {
              const p = payload?.[0]?.payload as EquityPointView | undefined;
              if (!active || !p) return null;
              return (
                <TooltipBox title={p.label}>
                  {formatMoney(p.equity, "equity")}
                  {p.underwater < 0 && (
                    <div className="font-normal text-neg-text">Drawdown {formatMoney(p.underwater, "pnl")}</div>
                  )}
                </TooltipBox>
              );
            }}
          />
          <Area
            yAxisId="dd"
            type="stepAfter"
            dataKey="underwater"
            stroke="none"
            fill="var(--neg)"
            fillOpacity={0.25}
            isAnimationActive={false}
          />
          <Area
            yAxisId="eq"
            type="monotone"
            dataKey="equity"
            stroke={color}
            strokeWidth={2.5}
            fill={`url(#${gid})`}
            dot={false}
            isAnimationActive={false}
          />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}

export interface ScatterPointView {
  id: string;
  label: string; // ticker
  x: number;
  y: number;
}

/**
 * Modelled vs realised: one dot per trade (x = modelled, y = realised), the y = x
 * diagonal dotted, dots above it beat the model (green) and below it missed (red).
 */
export function ModelScatter({
  data,
  height = 220,
  xLabel,
  yLabel,
  onSelect,
}: {
  data: ScatterPointView[];
  height?: number;
  xLabel: string;
  yLabel: string;
  onSelect?: (id: string) => void;
}) {
  const xs = data.map((d) => d.x);
  const ys = data.map((d) => d.y);
  const lo = Math.min(0, ...xs, ...ys);
  const hi = Math.max(0, ...xs, ...ys);
  const beat = data.filter((d) => d.y >= d.x);
  const missed = data.filter((d) => d.y < d.x);
  const tip = ({ active, payload }: { active?: boolean; payload?: ReadonlyArray<{ payload?: unknown }> }) => {
    const p = payload?.[0]?.payload as ScatterPointView | undefined;
    if (!active || !p) return null;
    return (
      <TooltipBox title={p.label}>
        {yLabel} {formatMoney(p.y, "pnl")}
        <div className="font-normal text-secondary">
          {xLabel} {formatMoney(p.x, "pnl")}
        </div>
      </TooltipBox>
    );
  };
  const click = (p: unknown) => {
    const id = (p as { id?: string; payload?: { id?: string } } | undefined)?.payload?.id;
    if (id && onSelect) onSelect(id);
  };
  return (
    <div style={{ height }} className="w-full" data-testid="model-scatter">
      <ResponsiveContainer width="100%" height="100%">
        <ScatterChart margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
          <CartesianGrid stroke="var(--border)" strokeDasharray="2 4" />
          <XAxis
            type="number"
            dataKey="x"
            domain={[lo, hi]}
            tickLine={false}
            axisLine={false}
            tickCount={3}
            tickFormatter={(v: number) => formatAxisMoney(v)}
            tick={{ fill: "var(--text-muted)", fontSize: 11 }}
          />
          <YAxis
            type="number"
            dataKey="y"
            domain={[lo, hi]}
            width={48}
            tickLine={false}
            axisLine={false}
            tickCount={3}
            tickFormatter={(v: number) => formatAxisMoney(v)}
            tick={{ fill: "var(--text-muted)", fontSize: 11 }}
          />
          <ZAxis range={[36, 36]} />
          <ReferenceLine
            segment={[
              { x: lo, y: lo },
              { x: hi, y: hi },
            ]}
            stroke="var(--text-muted)"
            strokeDasharray="2 4"
          />
          <ReferenceLine y={0} stroke="var(--border-input)" />
          <Tooltip cursor={false} content={tip} />
          <Scatter data={beat} fill="var(--pos)" isAnimationActive={false} onClick={click} cursor="pointer" />
          <Scatter data={missed} fill="var(--neg)" isAnimationActive={false} onClick={click} cursor="pointer" />
        </ScatterChart>
      </ResponsiveContainer>
    </div>
  );
}
