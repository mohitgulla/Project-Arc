import type { ReactNode } from "react";

import { useLayout } from "../lib/layout";
import { IconBack, IconClose } from "./icons";

/**
 * Detail view (§4, §6): a right-side panel on >=1280px; below that a full-page view with a
 * back button. The caller decides where it mounts (a route in E8.7b); *onClose* goes back.
 */
export function DetailPanel({
  title,
  onClose,
  children,
  inline = false,
}: {
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
  /** Render in place (kitchen sink) instead of fixed to the viewport. */
  inline?: boolean;
}) {
  const layout = useLayout();
  const side = layout === "desktop";
  const position = inline
    ? "relative"
    : side
      ? "fixed right-0 top-0 bottom-0 z-30 w-[440px]"
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
      <div className="min-h-0 flex-1 overflow-auto p-[var(--card-pad)]">{children}</div>
    </aside>
  );
}
