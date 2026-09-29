import { useId } from "react";
import { Area, AreaChart, ReferenceLine, ResponsiveContainer, YAxis } from "recharts";

/** ~80x28 trend, gradient fill, dotted reference line (§4). Colour by sign of change. */
export function Sparkline({
  values,
  reference,
  width = 80,
  height = 28,
}: {
  values: number[];
  reference?: number;
  width?: number;
  height?: number;
}) {
  const gid = `spark-${useId().replace(/:/g, "")}`;
  const data = values.map((v, i) => ({ i, v }));
  const first = values[0] ?? 0;
  const last = values[values.length - 1] ?? 0;
  const color = last >= (reference ?? first) ? "var(--pos)" : "var(--neg)";
  return (
    <div style={{ width, height }} aria-hidden="true">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 2, right: 1, bottom: 2, left: 1 }}>
          <defs>
            <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity={0.3} />
              <stop offset="100%" stopColor={color} stopOpacity={0} />
            </linearGradient>
          </defs>
          <YAxis hide domain={["dataMin", "dataMax"]} />
          <ReferenceLine
            y={reference ?? first}
            stroke="var(--text-muted)"
            strokeDasharray="1 3"
            strokeWidth={1}
          />
          <Area
            type="monotone"
            dataKey="v"
            stroke={color}
            strokeWidth={1.5}
            fill={`url(#${gid})`}
            isAnimationActive={false}
            dot={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
