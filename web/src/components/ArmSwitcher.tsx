import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import type { ReactNode } from "react";

import { armDot, bannerText, CONTROL, viewableArms, viewLabel, switcherLabel, armIsViewable, type ArmMeta } from "../lib/arm";
import { useArmState } from "../lib/armContext";
import { SWITCHER_NAME } from "../lib/experiments";
import { useControlMeta } from "../lib/useApi";

const MARGIN = 8;
const GAP = 4;

/**
 * E15.7 (D69, D92): the account switcher. A trigger showing the account under view
 * ("Arc Investor" for production, the owner's switcher exception to D108's "Production", or an
 * arm's treatment name) opens a menu of Arc Investor plus every running arm (`/api/meta.arms`,
 * always read from production's store). Choosing one sets `?arm=` (shareable); nothing is
 * persisted, so a fresh load is production.
 *
 * It lives only in the Overview status strip's account slot (its `children` are the trigger), the
 * Tower's one master account control: the choice applies to every page, which show the
 * ArmBanner while an arm is under view instead of a per-page switcher (D103).
 */
export function ArmSwitcher({
  children,
  className = "",
  title,
  testid = "account-switcher",
}: {
  children: ReactNode;
  className?: string;
  title?: string;
  testid?: string;
}) {
  const { arm, setArm } = useArmState();
  const meta = useControlMeta();
  const arms = viewableArms(meta.data?.arms);
  const label = switcherLabel(arm, meta.data?.arms);
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);
  const btn = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const id = useId();
  const close = useCallback(() => {
    setOpen(false);
    setPos(null);
  }, []);

  const place = useCallback(() => {
    if (!btn.current || !menu.current) return;
    const r = btn.current.getBoundingClientRect();
    const w = menu.current.offsetWidth;
    const vw = window.innerWidth;
    const left = Math.max(MARGIN, Math.min(r.left, vw - w - MARGIN));
    setPos({ left, top: r.bottom + GAP });
  }, []);

  useLayoutEffect(() => {
    if (open) place();
  }, [open, place]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        close();
        btn.current?.focus();
        return;
      }
      if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
      const items = Array.from(menu.current?.querySelectorAll<HTMLButtonElement>("[role=menuitemradio]") ?? []);
      if (items.length === 0) return;
      e.preventDefault();
      const i = items.indexOf(document.activeElement as HTMLButtonElement);
      const n = items.length;
      const next = i < 0 ? (e.key === "ArrowDown" ? 0 : n - 1) : (i + (e.key === "ArrowDown" ? 1 : n - 1)) % n;
      items[next]?.focus();
    };
    const onDown = (e: PointerEvent) => {
      const t = e.target as Node;
      if (!btn.current?.contains(t) && !menu.current?.contains(t)) close();
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onDown);
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, { capture: true, passive: true });
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onDown);
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, { capture: true });
    };
  }, [open, close, place]);

  // Focus the checked item once the menu is placed (it is visibility:hidden, so unfocusable,
  // until then): keyboard users land on the current account.
  const placed = pos !== null;
  useEffect(() => {
    if (open && placed) menu.current?.querySelector<HTMLButtonElement>('[aria-checked="true"]')?.focus();
  }, [open, placed]);

  const choose = (next: string | null) => {
    close();
    if (next !== arm) setArm(next);
  };

  const trigger = `arc-press w-full text-left enabled:hover:bg-hover ${className}`;

  return (
    <>
      <button
        ref={btn}
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        aria-label={`Account: ${label}. Switch account`}
        title={title ?? `Viewing ${label}`}
        data-testid={testid}
        data-arm={arm ?? CONTROL}
        onClick={() => (open ? close() : setOpen(true))}
        className={trigger}
      >
        {children}
      </button>
      {open && (
        <div
          ref={menu}
          id={id}
          role="menu"
          aria-label="Switch account"
          data-testid="arm-menu"
          className="fixed z-[60] min-w-[220px] max-w-[calc(100vw-16px)] rounded-control border border-line bg-card py-1 shadow-lg"
          style={pos ? { left: pos.left, top: pos.top } : { left: 0, top: 0, visibility: "hidden" }}
        >
          <ArmItem label={SWITCHER_NAME} sub="Production account" dot={armDot(null, arms)} checked={!arm} onPick={() => choose(null)} />
          {arms.length > 0 && <div className="arc-micro-header mt-1 border-t border-line px-3 pb-1 pt-2">Experiment Arms</div>}
          {arms.map((a) => (
            <ArmItem
              key={a.id}
              label={a.label}
              sub={a.store_age_s === null || a.store_age_s === undefined ? undefined : `Virtual account · ${ageText(a.store_age_s)}`}
              dot={armDot(a, arms)}
              checked={a.id === arm}
              onPick={() => choose(a.id)}
            />
          ))}
          {arms.length === 0 && (
            <p className="px-3 py-2 text-caption text-muted" data-testid="arm-menu-empty">
              {meta.isLoading ? "Loading…" : "No experiment arms running."}
            </p>
          )}
        </div>
      )}
    </>
  );
}

/** Small down chevron (SVG, so the trigger's text stays the account label only). */
export function Chevron() {
  return (
    <svg aria-hidden="true" width="10" height="10" viewBox="0 0 10 10" className="shrink-0 text-muted">
      <path d="M2 3.5 5 6.5 8 3.5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function ageText(s: number): string {
  if (s < 60) return "updated now";
  if (s < 3600) return `updated ${Math.floor(s / 60)}m ago`;
  if (s < 86_400) return `updated ${Math.floor(s / 3600)}h ago`;
  return `updated ${Math.floor(s / 86_400)}d ago`;
}

function ArmItem({
  label,
  sub,
  dot,
  checked,
  onPick,
}: {
  label: string;
  sub?: string;
  dot: string;
  checked: boolean;
  onPick: () => void;
}) {
  return (
    <button
      type="button"
      role="menuitemradio"
      aria-checked={checked}
      onClick={onPick}
      className={`flex w-full items-center gap-2 px-3 py-1.5 text-left text-caption hover:bg-hover focus-visible:bg-hover focus-visible:outline-none max-tablet:min-h-[44px] ${
        checked ? "font-semibold text-primary" : "text-secondary"
      }`}
    >
      <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: dot }} aria-hidden="true" />
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="truncate">{label}</span>
        {sub && <span className="truncate text-micro font-normal text-muted">{sub}</span>}
      </span>
      <span aria-hidden="true" className={`text-accent ${checked ? "" : "invisible"}`}>
        ✓
      </span>
    </button>
  );
}

/**
 * Persistent banner on every page while an arm is under view: what is being shown and a
 * one-tap Back to Arc Investor. An arm the API refuses (stopped, unknown) says so instead.
 */
export function ArmBanner() {
  const { arm, setArm } = useArmState();
  const meta = useControlMeta();
  if (!arm) return null;
  const arms: readonly ArmMeta[] | undefined = meta.data?.arms;
  const ok = armIsViewable(arm, arms);
  const label = viewLabel(arm, arms);
  const text = ok === false ? `${label} is not a running experiment arm — nothing to show` : bannerText(label);
  return (
    <div
      role="status"
      data-testid="arm-banner"
      data-arm={arm}
      className={`flex flex-wrap items-center gap-x-3 gap-y-1 border-b px-4 py-2 text-caption tablet:px-6 ${
        ok === false ? "border-neg bg-neg-bg text-neg-text" : "border-warn bg-header text-primary"
      }`}
    >
      <span className="h-2 w-2 shrink-0 rounded-full bg-warn" aria-hidden="true" />
      <span className="min-w-0 flex-1 [overflow-wrap:anywhere]">{text}</span>
      <button
        type="button"
        onClick={() => setArm(null)}
        data-testid="arm-back"
        className="arc-press arc-touch -my-2 inline-flex items-center justify-center rounded-control px-2 font-semibold text-accent hover:bg-hover tablet:min-h-0"
      >
        Back to {SWITCHER_NAME}
      </button>
    </div>
  );
}
