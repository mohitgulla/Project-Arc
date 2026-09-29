import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis } from "recharts";

import { formatMoney } from "../lib/format";
import { TooltipBox } from "./chartKit";

export interface StackSeries {
  key: string;
  label: string;
  color: string; // a token, e.g. "var(--series-1)"
}

/** Category-stacked bars, 1px gaps, negative segments allowed, no legend (tooltip) (§4). */
export function StackedBars({
  data,
  series,
  height = 200,
  format = (v: number) => formatMoney(v, "pnl"),
}: {
  data: Array<Record<string, number | string>>;
  series: StackSeries[];
  height?: number;
  format?: (v: number) => string;
}) {
  return (
    <div style={{ height }} className="w-full" data-testid="stacked-bars">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} stackOffset="sign" margin={{ top: 8, right: 4, bottom: 0, left: 4 }}>
          <XAxis
            dataKey="label"
            tickLine={false}
            axisLine={false}
            interval="preserveStartEnd"
            tick={{ fill: "var(--text-muted)", fontSize: 11 }}
          />
          <Tooltip
            cursor={{ fill: "var(--bg-hover)" }}
            content={({ active, payload, label }) => {
              if (!active || !payload?.length) return null;
              return (
                <TooltipBox title={String(label)}>
                  {payload.map((p) => {
                    const s = series.find((x) => x.key === p.dataKey);
                    return (
                      <div key={String(p.dataKey)} className="flex items-center gap-2">
                        <span className="h-2 w-2 rounded-full" style={{ background: s?.color }} />
                        <span className="font-normal text-secondary">{s?.label}</span>
                        <span className="ml-auto">{format(Number(p.value))}</span>
                      </div>
                    );
                  })}
                </TooltipBox>
              );
            }}
          />
          {series.map((s) => (
            <Bar
              key={s.key}
              dataKey={s.key}
              stackId="a"
              fill={s.color}
              stroke="var(--bg-card)"
              strokeWidth={1}
              isAnimationActive={false}
            />
          ))}
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
