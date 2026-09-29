import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { formatAxisMoney, formatMoney } from "../lib/format";
import { TooltipBox } from "./chartKit";

export interface BarPoint {
  label: string;
  /** null = a future slot: rendered empty so the axis is always full. */
  v: number | null;
}

/**
 * Monthly/daily bars: green above 0, red below, dotted gridlines, 2 Y ticks, a "Now"
 * marker (label + thin vertical line), future slots empty (§4).
 */
export function DivergingBars({
  data,
  nowLabel,
  height = 200,
  format = (v: number) => formatMoney(v, "pnl"),
}: {
  data: BarPoint[];
  nowLabel?: string;
  height?: number;
  format?: (v: number) => string;
}) {
  const vals = data.map((d) => d.v ?? 0);
  const max = Math.max(0, ...vals);
  const min = Math.min(0, ...vals);
  const ticks = [min < 0 ? min : 0, max > 0 ? max : 0].filter((v, i, a) => a.indexOf(v) === i);
  return (
    <div style={{ height }} className="w-full" data-testid="diverging-bars">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 22, right: 8, bottom: 0, left: 0 }} barCategoryGap="18%">
          <CartesianGrid vertical={false} stroke="var(--border)" strokeDasharray="2 4" />
          <XAxis
            dataKey="label"
            tickLine={false}
            axisLine={false}
            interval="preserveStartEnd"
            tick={{ fill: "var(--text-muted)", fontSize: 11 }}
          />
          <YAxis
            ticks={ticks}
            width={48}
            tickLine={false}
            axisLine={false}
            tickFormatter={(v: number) => formatAxisMoney(v)}
            tick={{ fill: "var(--text-muted)", fontSize: 11 }}
          />
          <ReferenceLine y={0} stroke="var(--border-input)" strokeWidth={1} />
          {nowLabel && (
            <ReferenceLine
              x={nowLabel}
              stroke="var(--text-secondary)"
              strokeWidth={1}
              label={({ viewBox }: { viewBox?: { x?: number; y?: number } }) => {
                const x = viewBox?.x ?? 0;
                const y = (viewBox?.y ?? 0) - 18;
                return (
                  <g>
                    <rect x={x - 17} y={y} width={34} height={16} rx={4} fill="var(--now-label)" />
                    <text
                      x={x}
                      y={y + 11.5}
                      textAnchor="middle"
                      fontSize={10}
                      fontWeight={700}
                      fill="var(--now-label-text)"
                    >
                      Now
                    </text>
                  </g>
                );
              }}
            />
          )}
          <Tooltip
            cursor={{ fill: "var(--bg-hover)" }}
            content={({ active, payload }) => {
              const p = payload?.[0]?.payload as BarPoint | undefined;
              if (!active || !p || p.v === null) return null;
              return <TooltipBox title={p.label}>{format(p.v)}</TooltipBox>;
            }}
          />
          <Bar dataKey="v" radius={[3, 3, 3, 3]} isAnimationActive={false}>
            {data.map((d) => (
              <Cell key={d.label} fill={(d.v ?? 0) >= 0 ? "var(--pos)" : "var(--neg)"} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
