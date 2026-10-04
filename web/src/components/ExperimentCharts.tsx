import {
  Area,
  ComposedChart,
  Legend,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { CumView, CurveView } from "../lib/experiments";
import { formatAxisMoney, formatMoney } from "../lib/format";
import { TooltipBox } from "./chartKit";

const AXIS_TICK = { fill: "var(--text-muted)", fontSize: 11 };

/** Both arms' equity from t0 (legacy book excluded): control dotted, treatment solid. */
export function ArmEquityChart({ data, height = 220 }: { data: CurveView[]; height?: number }) {
  return (
    <div style={{ height }} className="w-full" data-testid="arm-equity-chart">
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
          <XAxis dataKey="label" tickLine={false} axisLine={false} interval="preserveStartEnd" tick={AXIS_TICK} />
          <YAxis
            domain={["dataMin", "dataMax"]}
            width={56}
            tickLine={false}
            axisLine={false}
            tickCount={3}
            tickFormatter={(v: number) => formatAxisMoney(v)}
            tick={AXIS_TICK}
          />
          <Tooltip
            cursor={{ stroke: "var(--border-input)", strokeWidth: 1 }}
            content={({ active, payload }) => {
              const p = payload?.[0]?.payload as CurveView | undefined;
              if (!active || !p) return null;
              return (
                <TooltipBox title={p.label}>
                  Treatment {formatMoney(p.treatment, "equity")}
                  <div className="font-normal text-secondary">Control {formatMoney(p.control, "equity")}</div>
                </TooltipBox>
              );
            }}
          />
          <Legend iconType="plainline" wrapperStyle={{ fontSize: 11, color: "var(--text-secondary)" }} />
          <Line
            name="Control"
            dataKey="control"
            stroke="var(--text-muted)"
            strokeDasharray="4 4"
            strokeWidth={2}
            dot={false}
            isAnimationActive={false}
          />
          <Line
            name="Treatment"
            dataKey="treatment"
            stroke="var(--accent)"
            strokeWidth={2.5}
            dot={false}
            isAnimationActive={false}
          />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}

/**
 * Cumulative paired difference d_t (% of t0 equity) with its always-valid confidence
 * band (n × the CI on the mean after n sessions); the 0 line dotted.
 */
export function CumulativeDiffChart({ data, height = 200 }: { data: CumView[]; height?: number }) {
  const fmt = (v: number) => `${v >= 0 ? "+" : "\u2212"}${Math.abs(v).toFixed(2)}%`;
  return (
    <div style={{ height }} className="w-full" data-testid="cumulative-diff-chart">
      <ResponsiveContainer width="100%" height="100%">
        <ComposedChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
          <XAxis dataKey="label" tickLine={false} axisLine={false} interval="preserveStartEnd" tick={AXIS_TICK} />
          <YAxis width={56} tickLine={false} axisLine={false} tickCount={3} tickFormatter={fmt} tick={AXIS_TICK} />
          <ReferenceLine y={0} stroke="var(--text-muted)" strokeDasharray="2 4" />
          <Tooltip
            cursor={{ stroke: "var(--border-input)", strokeWidth: 1 }}
            content={({ active, payload }) => {
              const p = payload?.[0]?.payload as CumView | undefined;
              if (!active || !p) return null;
              return (
                <TooltipBox title={p.label}>
                  Σ d {fmt(p.cum)}
                  <div className="font-normal text-secondary">
                    {p.band ? `band [${fmt(p.band[0])}, ${fmt(p.band[1])}]` : "band n/a (too few sessions)"}
                  </div>
                </TooltipBox>
              );
            }}
          />
          <Area
            dataKey="band"
            stroke="none"
            fill="var(--accent)"
            fillOpacity={0.15}
            isAnimationActive={false}
            connectNulls={false}
          />
          <Line dataKey="cum" stroke="var(--accent)" strokeWidth={2.5} dot={false} isAnimationActive={false} />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}
