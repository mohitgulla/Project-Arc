import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

const GAP = 6;
const MARGIN = 8;

/** `(hover: hover)` devices open on hover; touch devices open on tap only. */
function canHover(): boolean {
  return typeof window !== "undefined" && Boolean(window.matchMedia?.("(hover: hover)").matches);
}

/**
 * Small anchored popover shared by InfoTip and the freshness badge (TOWER_DESIGN §10): opens
 * on hover (hover devices), focus and tap; Esc, an outside tap, or scrolling the trigger out of
 * view close it. Rendered
 * `position: fixed` and clamped to the viewport, so it never widens the page at 390/520 px.
 */
export function Popover({
  trigger,
  label,
  children,
  className = "",
  testid,
  triggerProps,
}: {
  /** Visible content of the trigger button. */
  trigger: ReactNode;
  /** Accessible name of the trigger. */
  label: string;
  children: ReactNode;
  className?: string;
  testid?: string;
  triggerProps?: Record<string, string | boolean | undefined>;
}) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);
  const btn = useRef<HTMLButtonElement>(null);
  const pop = useRef<HTMLDivElement>(null);
  // How it opened: a hover/focus open is "peeked", so a click pins it rather than toggling it
  // shut; `skipFocus` stops Esc's focus-return from reopening it.
  const via = useRef<"peek" | "click" | null>(null);
  const skipFocus = useRef(false);
  const id = useId();
  const close = useCallback(() => {
    via.current = null;
    setOpen(false);
  }, []);
  const peek = () => {
    if (open) return;
    via.current = "peek";
    setOpen(true);
  };

  // Anchor the popover under (or above) the trigger, clamped to the viewport.
  const place = useCallback(() => {
    if (!btn.current || !pop.current) return;
    const r = btn.current.getBoundingClientRect();
    const w = pop.current.offsetWidth;
    const h = pop.current.offsetHeight;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const left = Math.max(MARGIN, Math.min(r.left, vw - w - MARGIN));
    const below = r.bottom + GAP;
    const top = below + h > vh - MARGIN && r.top - GAP - h > MARGIN ? r.top - GAP - h : below;
    setPos({ left, top });
  }, []);

  useLayoutEffect(() => {
    if (open) place();
  }, [open, place]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        close();
        skipFocus.current = true;
        btn.current?.focus();
        skipFocus.current = false;
      }
    };
    const onDown = (e: PointerEvent) => {
      const t = e.target as Node;
      if (!btn.current?.contains(t) && !pop.current?.contains(t)) close();
    };
    // Scrolling follows the trigger (focus() scrolls it into view right as it opens); the
    // popover closes only once the trigger has left the viewport.
    const onScroll = () => {
      const r = btn.current?.getBoundingClientRect();
      if (!r || r.bottom < 0 || r.top > window.innerHeight) close();
      else place();
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onDown);
    window.addEventListener("scroll", onScroll, { capture: true, passive: true });
    window.addEventListener("resize", place);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onDown);
      window.removeEventListener("scroll", onScroll, { capture: true });
      window.removeEventListener("resize", place);
    };
  }, [open, close, place]);

  return (
    <span className="relative inline-flex" onMouseLeave={() => canHover() && via.current === "peek" && close()}>
      <button
        ref={btn}
        type="button"
        aria-label={label}
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        data-testid={testid}
        onClick={() => {
          if (open && via.current === "click") return close();
          via.current = "click";
          setOpen(true);
        }}
        onMouseEnter={() => canHover() && peek()}
        onFocus={(e) => !skipFocus.current && e.currentTarget.matches(":focus-visible") && peek()}
        onBlur={(e) => {
          if (!pop.current?.contains(e.relatedTarget as Node | null)) close();
        }}
        className={`arc-hit ${className}`}
        {...triggerProps}
      >
        {trigger}
      </button>
      {open && (
        <div
          ref={pop}
          id={id}
          role="tooltip"
          className="arc-popover"
          style={pos ? { left: pos.left, top: pos.top } : { left: 0, top: 0, visibility: "hidden" }}
        >
          {children}
        </div>
      )}
    </span>
  );
}
