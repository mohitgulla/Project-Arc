import type { ReactNode } from "react";
import { Link } from "react-router-dom";

/**
 * The card surface (§2): bg-card, 1px border, r-card, padding 24/16, no shadow. Title 16px
 * semibold top-left; optional action link top-right (`TRADES ↗`).
 */
export function Card({
  title,
  action,
  children,
  className = "",
  asOf,
}: {
  title?: ReactNode;
  action?: { label: string; to: string };
  children?: ReactNode;
  className?: string;
  asOf?: ReactNode;
}) {
  return (
    <section className={`arc-card min-w-0 ${className}`}>
      {(title || action) && (
        <header className="mb-3 flex items-start justify-between gap-3">
          {title && <h2 className="text-title text-title">{title}</h2>}
          {action && (
            <Link to={action.to} className="arc-action whitespace-nowrap">
              {action.label} ↗
            </Link>
          )}
        </header>
      )}
      {children}
      {asOf && <footer className="mt-3 text-micro text-muted">{asOf}</footer>}
    </section>
  );
}
