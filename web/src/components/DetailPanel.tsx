import type { ReactNode } from "react";

import { useLayout } from "../lib/layout";
import { IconBack, IconClose } from "./icons";

/**
 * Detail view (§4, §6): a right-side panel on >=1280px; below that a full-page view with a
 * back button. The caller decides where it mounts (a route in E8.7b); *onClose* goes back.
 * The scrolling body carries `data-detail-scroll`. With *pinned* the body does not scroll: the
 * child lays out a fixed top section plus its own `data-detail-scroll` scroller (trade detail,
 * D50), so the top never moves, without relying on `position: sticky` (iPhone Safari).
 */
export function DetailPanel({
  title,
  onClose,
  children,
  inline = false,
  wide = false,
  pinned = false,
}: {
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
  /** Render in place (kitchen sink) instead of fixed to the viewport. */
  inline?: boolean;
  /** 600 px side panel instead of 440 (trade detail: the stat strip is one row, E8.8f). */
  wide?: boolean;
  /** The child owns scrolling: a non-scrolling flex column body, no padding. */
  pinned?: boolean;
}) {
  const layout = useLayout();
  const side = layout === "desktop";
  const position = inline
    ? "relative"
    : side
      ? `fixed right-0 top-0 bottom-0 z-30 ${wide ? "w-[600px]" : "w-[440px]"}`
      : "fixed inset-0 z-30";
  return (
    <aside
      aria-label="Detail"
      data-mode={side ? "panel" : "page"}
      className={`${position} flex flex-col border-line bg-card ${side ? "border-l" : ""} ${inline ? "rounded-card border" : ""}`}
    >
      <header className="flex h-header-h shrink-0 items-center gap-2 border-b border-line px-4">
        {!side && (
          <button type="button" onClick={onClose} aria-label="Back" className="arc-touch -ml-3 flex items-center justify-center">
            <IconBack />
          </button>
        )}
        <h2 className="min-w-0 flex-1 truncate text-title text-title">{title}</h2>
        {side && (
          <button type="button" onClick={onClose} aria-label="Close" className="flex h-8 w-8 items-center justify-center text-muted hover:text-primary">
            <IconClose />
          </button>
        )}
      </header>
      {pinned ? (
        <div className="flex min-h-0 flex-1 flex-col">{children}</div>
      ) : (
        <div data-detail-scroll className="min-h-0 flex-1 overflow-auto p-[var(--card-pad)]">
          {children}
        </div>
      )}
    </aside>
  );
}
