import type { ReactNode } from "react";

import { Card } from "./Card";
import type { FreshnessProp } from "./Freshness";

/** Hero number, optional ChangePill, comparison line, optional child chart (§4). */
export function StatCard({
  title,
  value,
  change,
  comparison,
  action,
  freshness,
  subtitle,
  headerExtra,
  children,
}: {
  title: ReactNode;
  value: ReactNode;
  change?: ReactNode;
  comparison?: ReactNode;
  action?: { label: string; to: string };
  freshness?: FreshnessProp;
  subtitle?: ReactNode;
  headerExtra?: ReactNode;
  children?: ReactNode;
}) {
  return (
    <Card title={title} action={action} freshness={freshness} subtitle={subtitle} headerExtra={headerExtra}>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="text-hero font-semibold text-title tabular-nums">{value}</span>
        {change}
      </div>
      {comparison && <p className="mt-1 text-caption text-secondary">{comparison}</p>}
      {children && <div className="mt-4">{children}</div>}
    </Card>
  );
}
