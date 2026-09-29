import { useId, useState } from "react";
import type { ReactNode } from "react";

/** Collapsible header `▶/▼ Title` with a right-side sort/filter control (§4). */
export function Section({
  title,
  control,
  defaultOpen = true,
  children,
}: {
  title: ReactNode;
  control?: ReactNode;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const id = useId();
  return (
    <section className="min-w-0">
      <header className="flex items-center justify-between gap-3 py-2">
        <button
          type="button"
          aria-expanded={open}
          aria-controls={id}
          onClick={() => setOpen(!open)}
          className="flex min-h-[32px] items-center gap-2 text-title text-title"
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
