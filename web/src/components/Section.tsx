import { useId, useState } from "react";
import type { ReactNode } from "react";
import { useLocation } from "react-router-dom";

const PREFIX = "arc.section";

/** `arc.section:<page>:<section>`; the page is the first path segment (`/ops/runs/x` -> `ops`). */
export function sectionStorageKey(pathname: string, section: string): string {
  const page = pathname.split("/").filter(Boolean)[0] ?? "overview";
  const slug = section
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
  return `${PREFIX}:${page}:${slug}`;
}

/** Stored open state, or null on a first visit (then `defaultOpen` applies). */
export function readSectionOpen(key: string): boolean | null {
  try {
    const v = localStorage.getItem(key);
    return v === "1" ? true : v === "0" ? false : null;
  } catch {
    return null;
  }
}

export function writeSectionOpen(key: string, open: boolean): void {
  try {
    localStorage.setItem(key, open ? "1" : "0");
  } catch {
    /* private mode: remembered for this view only */
  }
}

/**
 * Collapsible header `▶/▼ Title` with a right-side sort/filter control (§4). Remembers
 * open/closed per page + section in localStorage (§10); `defaultOpen` applies only on the first
 * visit. The key comes from `storageKey`, else from a string title; a node title without a
 * `storageKey` is not remembered.
 */
export function Section({
  title,
  control,
  defaultOpen = true,
  storageKey,
  children,
}: {
  title: ReactNode;
  control?: ReactNode;
  defaultOpen?: boolean;
  storageKey?: string;
  children: ReactNode;
}) {
  const { pathname } = useLocation();
  const name = storageKey ?? (typeof title === "string" ? title : null);
  const key = name ? sectionStorageKey(pathname, name) : null;
  const [open, setOpen] = useState(() => (key ? readSectionOpen(key) : null) ?? defaultOpen);
  const id = useId();
  const toggle = () => {
    const next = !open;
    setOpen(next);
    if (key) writeSectionOpen(key, next);
  };
  return (
    <section className="min-w-0">
      <header className="flex items-center justify-between gap-3 py-2">
        <button
          type="button"
          aria-expanded={open}
          aria-controls={id}
          onClick={toggle}
          className="arc-title flex min-h-[32px] items-center gap-2 text-left text-title text-title max-tablet:min-h-[44px]"
        >
          <span className="w-3 text-caption text-muted" aria-hidden="true">
            {open ? "▼" : "▶"}
          </span>
          {title}
        </button>
        {control}
      </header>
      <div id={id} hidden={!open}>
        {children}
      </div>
    </section>
  );
}
