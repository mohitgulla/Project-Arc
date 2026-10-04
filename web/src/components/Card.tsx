import type { ReactNode } from "react";
import { Link } from "react-router-dom";

import { Freshness, type FreshnessProp } from "./Freshness";

/**
 * The card surface (§2): bg-card, 1px border, r-card, padding 24/16, no shadow. Header line
 * (§10): Title Case title, then the one freshness badge, then optional extras (an InfoTip, a
 * compact SegmentedControl); the action link (`VIEW ALL ↗`) sits top-right. An optional
 * one-line `subtitle` carries non-time footnotes (`last 24 h`, `local count`).
 */
export function Card({
  title,
  action,
  children,
  className = "",
  freshness,
  subtitle,
  headerExtra,
  testid,
}: {
  title?: ReactNode;
  action?: { label: string; to: string };
  children?: ReactNode;
  className?: string;
  freshness?: FreshnessProp;
  subtitle?: ReactNode;
  headerExtra?: ReactNode;
  testid?: string;
}) {
  const hasHeader = title || action || freshness || headerExtra;
  return (
    <section className={`arc-card min-w-0 ${className}`} data-testid={testid}>
      {hasHeader && (
        <header className={`${subtitle ? "mb-1" : "mb-3"} flex items-start justify-between gap-3`}>
          <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
            {title && <h2 className="arc-title text-title text-title">{title}</h2>}
            {freshness && <Freshness {...freshness} />}
            {headerExtra}
          </div>
          {action && (
            <Link to={action.to} className="arc-action arc-hit shrink-0 whitespace-nowrap">
              {action.label} ↗
            </Link>
          )}
        </header>
      )}
      {subtitle && (
        <p className="mb-3 truncate text-micro text-muted" data-subtext>
          {subtitle}
        </p>
      )}
      {children}
    </section>
  );
}
