import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import type { Metric } from "../lib/format";
import { ChangePill } from "./ChangePill";
import { Sparkline } from "./Sparkline";

export interface TileData {
  ticker: string;
  name: string;
  values: number[];
  change: number;
  metric?: Metric;
  to?: string;
  /** Tone for the name line (D87: Movers colour Bullish / Bearish). Default muted. */
  nameClassName?: string;
  /** D88: an extra line under the name (Movers: the stock's own day change). */
  sub?: ReactNode;
}

/** ~116x152 mover tile: ticker, name, Sparkline, ChangePill (§4). */
export function Tile({ ticker, name, values, change, metric = "pnl", to, nameClassName = "text-muted", sub }: TileData) {
  const body = (
    <div className="flex h-[152px] w-[116px] shrink-0 flex-col justify-between rounded-control border border-line bg-card p-3 hover:bg-hover">
      <div className="min-w-0">
        <div className="font-semibold text-title">{ticker}</div>
        <div className={`truncate text-micro ${nameClassName}`} title={name} data-testid="tile-name">
          {name}
        </div>
        {sub !== undefined && (
          <div className="truncate text-micro tabular-nums" data-testid="tile-sub">
            {sub}
          </div>
        )}
      </div>
      <Sparkline values={values} width={92} height={28} />
      <ChangePill value={change} metric={metric} />
    </div>
  );
  return to ? (
    <Link to={to} className="shrink-0 snap-start">
      {body}
    </Link>
  ) : (
    <div className="shrink-0 snap-start">{body}</div>
  );
}

/** Horizontal scroll container for Tiles; snaps tile by tile on touch (E8.8b). */
export function TileRow({ children }: { children: ReactNode }) {
  return (
    <div className="arc-scroll-x -mx-1 flex snap-x snap-mandatory scroll-px-1 gap-3 px-1 pb-1" data-scroll-x data-testid="tile-row">
      {children}
    </div>
  );
}
