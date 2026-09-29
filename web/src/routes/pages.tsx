import { Link } from "react-router-dom";

import { Card } from "../components/Card";
import { EmptyState } from "../components/EmptyState";

/** Page placeholder until its card lands (E8.7b-d). Renders inside the Shell. */
export function Placeholder({ title, card, blurb }: { title: string; card: string; blurb: string }) {
  return (
    <div className="grid gap-6 desktop:gap-10">
      <Card title={title}>
        <EmptyState caption={`${blurb} Coming in ${card}.`}>
          <Link to="/kitchen-sink" className="arc-action">
            Component kitchen sink ↗
          </Link>
        </EmptyState>
      </Card>
    </div>
  );
}

export const TradesPage = () => (
  <Placeholder title="Trades" card="E8.7b" blurb="Every proposal from Scout to close, with its decision trail." />
);
export const PerformancePage = () => (
  <Placeholder title="Performance" card="E8.7c" blurb="Day / MTD / YTD P&L, costs and scorecard." />
);
export const OpsPage = () => (
  <Placeholder title="Ops" card="E8.7d" blurb="Ticks, routine runs, halts, alerts and gate violations." />
);
export const NotFoundPage = () => (
  <Card title="Not found">
    <EmptyState caption="No page here.">
      <Link to="/" className="arc-action">
        Overview ↗
      </Link>
    </EmptyState>
  </Card>
);
