import { useEffect, useState } from "react";
import type { ComponentType, ReactNode, SVGProps } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";

import { apiGet, num } from "../lib/api";
import { useLayout } from "../lib/layout";
import { REFRESH_CHOICES, useSettings } from "../lib/settings";
import { useMeta, useSearch, useSnapshot } from "../lib/useApi";
import { AsOfBadge } from "./AsOfBadge";
import {
  IconClose,
  IconDocs,
  IconMoon,
  IconOps,
  IconOverview,
  IconPerformance,
  IconPositions,
  IconSearch,
  IconSettings,
  IconSun,
  IconTrades,
} from "./icons";
import { Money } from "./Money";

type Icon = ComponentType<SVGProps<SVGSVGElement>>;

export const NAV: Array<{ to: string; label: string; icon: Icon }> = [
  { to: "/", label: "Overview", icon: IconOverview },
  { to: "/trades", label: "Trades", icon: IconTrades },
  { to: "/positions", label: "Positions", icon: IconPositions },
  { to: "/performance", label: "Performance", icon: IconPerformance },
  { to: "/ops", label: "Ops", icon: IconOps },
];

const TITLES: Record<string, string> = {
  "/": "Overview",
  "/trades": "Trades",
  "/positions": "Positions",
  "/performance": "Performance",
  "/ops": "Ops",
  "/kitchen-sink": "Kitchen sink",
  "/settings": "Settings",
};

export function pageTitle(pathname: string): string {
  const base = `/${pathname.split("/")[1] ?? ""}`;
  return TITLES[base] ?? "Arc";
}

// ---------------------------------------------------------------------------
// Header pieces
// ---------------------------------------------------------------------------

export function ThemeToggle() {
  const { theme, toggleTheme } = useSettings();
  const next = theme === "dark" ? "light" : "dark";
  return (
    <button
      type="button"
      onClick={toggleTheme}
      aria-label={`Switch to ${next} theme`}
      title={`Switch to ${next} theme`}
      data-testid="theme-toggle"
      className="arc-touch flex items-center justify-center rounded-control text-secondary hover:bg-hover hover:text-primary tablet:h-8 tablet:min-h-0 tablet:w-8 tablet:min-w-0"
    >
      {theme === "dark" ? <IconSun /> : <IconMoon />}
    </button>
  );
}

/**
 * Global search (E8.7b): ticker / hash prefix / run id / chain id / structure id. Enter
 * opens the first match; the dropdown lists every match (`GET /api/search`).
 */
export function SearchInput({ autoFocus = false, onDone }: { autoFocus?: boolean; onDone?: () => void }) {
  const [text, setText] = useState("");
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const navigate = useNavigate();
  const res = useSearch(q);
  // Debounce: query 200 ms after the last keystroke.
  useEffect(() => {
    const id = setTimeout(() => setQ(text.trim()), 200);
    return () => clearTimeout(id);
  }, [text]);
  const matches = q && res.data?.q === q ? res.data.matches : [];
  const go = (route: string) => {
    setOpen(false);
    setText("");
    onDone?.();
    navigate(route);
  };
  return (
    <div className="relative w-full" data-testid="global-search">
      <label className="relative flex w-full items-center">
        <IconSearch className="pointer-events-none absolute left-2.5 text-muted" width={16} height={16} />
        <input
          type="search"
          autoFocus={autoFocus}
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          onBlur={() => setTimeout(() => setOpen(false), 150)}
          onKeyDown={async (e) => {
            if (e.key === "Escape") setOpen(false);
            if (e.key !== "Enter" || !text.trim()) return;
            const term = text.trim();
            const hit =
              res.data?.q === term
                ? res.data.matches[0]
                : (await apiGet("/api/search", { query: { q: term } }).catch(() => null))?.matches[0];
            if (hit) go(hit.route);
          }}
          placeholder="Search ticker, hash, run id"
          aria-label="Search"
          aria-expanded={open && matches.length > 0}
          className="h-8 w-full rounded-control border border-line-input bg-control pl-8 pr-2 text-caption text-primary placeholder:text-muted max-tablet:h-11"
        />
      </label>
      {open && q && (
        <ul
          role="listbox"
          aria-label="Search results"
          className="absolute left-0 right-0 top-full z-50 mt-1 max-h-80 overflow-auto rounded-control border border-line bg-card py-1"
        >
          {matches.length === 0 ? (
            <li className="px-3 py-2 text-caption text-muted">{res.isFetching ? "Searching…" : "No matches"}</li>
          ) : (
            matches.map((m) => (
              <li key={`${m.kind}:${m.id}`} role="option" aria-selected={false}>
                <button
                  type="button"
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => go(m.route)}
                  className="flex w-full items-center gap-2 px-3 py-2 text-left text-caption hover:bg-hover max-tablet:min-h-[44px]"
                >
                  <span className="rounded-label bg-control px-1.5 text-micro uppercase text-muted">{m.kind}</span>
                  <span className="truncate text-primary">{m.label}</span>
                </button>
              </li>
            ))
          )}
        </ul>
      )}
    </div>
  );
}

/**
 * Header freshness: the snapshot's `as_of`, held back to the last `tick` heartbeat when that
 * is older, judged against the tick cadence from /api/meta. It warns when either polling or
 * the scheduler stops.
 */
export function HeaderAsOf({ compact = false }: { compact?: boolean }) {
  const meta = useMeta();
  const snap = useSnapshot();
  const asOf = snap.data?.as_of;
  const tickAt = snap.data?.ops.tick_at ?? null;
  const at = asOf && tickAt && tickAt < asOf ? tickAt : asOf;
  const cadence = meta.data?.cadences.tick?.every_s;
  if (snap.isError) {
    return (
      <span className="rounded-full border border-warn px-2 py-0.5 text-micro font-semibold text-warn">
        API unavailable
      </span>
    );
  }
  return <AsOfBadge at={at} cadenceS={cadence} label="last tick" compact={compact} />;
}

// ---------------------------------------------------------------------------
// Settings (client-side only)
// ---------------------------------------------------------------------------

export function SettingsPanel() {
  const s = useSettings();
  const row = "flex items-center justify-between gap-4 py-2";
  const seg = (on: boolean) =>
    `min-h-[30px] rounded-pill px-3 text-caption max-tablet:min-h-[44px] ${
      on ? "bg-range-active font-bold text-[color:var(--range-active-text)]" : "text-secondary hover:bg-hover"
    }`;
  return (
    <div className="divide-y divide-line text-body">
      <div className={row}>
        <span className="text-secondary">Theme</span>
        <div className="flex gap-1 rounded-control bg-control p-1">
          {(["system", "light", "dark"] as const).map((t) => (
            <button key={t} type="button" className={seg(s.themeChoice === t)} onClick={() => s.setThemeChoice(t)}>
              {t[0]!.toUpperCase() + t.slice(1)}
            </button>
          ))}
        </div>
      </div>
      <div className={row}>
        <span className="text-secondary">Density</span>
        <div className="flex gap-1 rounded-control bg-control p-1">
          {(["comfortable", "compact"] as const).map((d) => (
            <button key={d} type="button" className={seg(s.density === d)} onClick={() => s.setDensity(d)}>
              {d[0]!.toUpperCase() + d.slice(1)}
            </button>
          ))}
        </div>
      </div>
      <div className={row}>
        <span className="text-secondary">Refresh</span>
        <div className="flex gap-1 rounded-control bg-control p-1">
          {REFRESH_CHOICES.map((r) => (
            <button key={r} type="button" className={seg(s.refreshS === r)} onClick={() => s.setRefreshS(r)}>
              {r}s
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

function SettingsDialog({ onClose }: { onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" onClick={onClose}>
      <div
        role="dialog"
        aria-label="Settings"
        className="arc-card w-full max-w-md"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-title text-title">Settings</h2>
          <button type="button" aria-label="Close" onClick={onClose} className="arc-touch flex items-center justify-center">
            <IconClose />
          </button>
        </div>
        <SettingsPanel />
        <p className="mt-3 text-micro text-muted">Saved in this browser only. The tower is read-only.</p>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Navigation: sidebar (desktop), icon rail (tablet), bottom tab bar (mobile)
// ---------------------------------------------------------------------------

function SidebarRow({ dot, name, value }: { dot: string; name: string; value: ReactNode }) {
  return (
    <div className="flex h-[30px] items-center gap-2 px-3 text-caption">
      <span className="h-2 w-2 rounded-full" style={{ background: dot }} />
      <span className="flex-1 truncate text-secondary">{name}</span>
      <span className="font-semibold text-primary tabular-nums">{value}</span>
    </div>
  );
}

function AccountBlock() {
  const meta = useMeta();
  const snap = useSnapshot();
  const equity = num(snap.data?.pnl.intraday_equity ?? snap.data?.pnl.equity);
  const env = meta.data?.env ?? "paper";
  return (
    <div className="mt-6">
      <div className="arc-micro-header mb-1 px-3">Account</div>
      <SidebarRow dot="var(--accent)" name={`Alpaca ${env}`} value={meta.data?.account_profile ?? "—"} />
      <SidebarRow dot="var(--pos)" name="Equity" value={equity === null ? "—" : <Money value={equity} kind="equity" />} />
      {/* Buying power arrives with the E5.3a monitor account fields. */}
      <SidebarRow dot="var(--series-4)" name="Buying power" value="—" />
    </div>
  );
}

function Sidebar({ onSettings }: { onSettings: () => void }) {
  return (
    <nav
      aria-label="Main"
      className="fixed inset-y-0 left-0 z-20 flex w-sidebar-w flex-col border-r border-line bg-sidebar px-3 py-4"
    >
      <div className="mb-5 flex items-center gap-2 px-3">
        <span className="h-3 w-3 rounded-full bg-accent" />
        <span className="text-title font-bold text-title">Arc</span>
      </div>
      <ul className="flex flex-col gap-0.5">
        {NAV.map((n) => (
          <li key={n.to}>
            <NavLink
              to={n.to}
              end={n.to === "/"}
              className={({ isActive }) =>
                `flex h-[30px] items-center gap-2.5 rounded-control px-3 text-body ${
                  isActive ? "bg-accent-bar font-semibold text-white" : "text-secondary hover:bg-hover hover:text-primary"
                }`
              }
            >
              <n.icon width={16} height={16} />
              {n.label}
            </NavLink>
          </li>
        ))}
      </ul>
      <AccountBlock />
      <div className="mt-auto flex flex-col gap-0.5 border-t border-line pt-3">
        <NavLink to="/kitchen-sink" className="flex h-[30px] items-center gap-2.5 rounded-control px-3 text-body text-secondary hover:bg-hover">
          <IconDocs width={16} height={16} /> Docs
        </NavLink>
        <button
          type="button"
          onClick={onSettings}
          className="flex h-[30px] items-center gap-2.5 rounded-control px-3 text-left text-body text-secondary hover:bg-hover"
        >
          <IconSettings width={16} height={16} /> Settings
        </button>
      </div>
    </nav>
  );
}

function IconRail({ onSettings }: { onSettings: () => void }) {
  return (
    <nav
      aria-label="Main"
      className="fixed inset-y-0 left-0 z-20 flex w-rail flex-col items-center border-r border-line bg-sidebar py-3"
    >
      <span className="mb-4 mt-1 h-3 w-3 rounded-full bg-accent" aria-label="Arc" />
      <ul className="flex flex-col gap-1">
        {NAV.map((n) => (
          <li key={n.to}>
            <NavLink
              to={n.to}
              end={n.to === "/"}
              title={n.label}
              aria-label={n.label}
              className={({ isActive }) =>
                `flex h-11 w-11 items-center justify-center rounded-control ${
                  isActive ? "bg-accent-bar text-white" : "text-secondary hover:bg-hover"
                }`
              }
            >
              <n.icon />
            </NavLink>
          </li>
        ))}
      </ul>
      <button
        type="button"
        title="Settings"
        aria-label="Settings"
        onClick={onSettings}
        className="mt-auto flex h-11 w-11 items-center justify-center rounded-control text-secondary hover:bg-hover"
      >
        <IconSettings />
      </button>
    </nav>
  );
}

function TabBar() {
  return (
    <nav
      aria-label="Main"
      className="fixed inset-x-0 bottom-0 z-20 border-t border-line bg-sidebar pb-[env(safe-area-inset-bottom)]"
    >
      <ul className="grid h-tabbar grid-cols-5">
        {NAV.map((n) => (
          <li key={n.to}>
            <NavLink
              to={n.to}
              end={n.to === "/"}
              className={({ isActive }) =>
                `flex h-full flex-col items-center justify-center gap-0.5 text-micro ${
                  isActive ? "text-accent" : "text-muted"
                }`
              }
            >
              <n.icon />
              {n.label}
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  );
}

// ---------------------------------------------------------------------------
// Shell
// ---------------------------------------------------------------------------

/**
 * App frame (§4-§6): sidebar on desktop, 56px icon rail on tablet, 5-item bottom tab bar
 * on mobile; header with page title, AsOfBadge, theme toggle and global search (an icon on
 * mobile).
 */
export function Shell({ children }: { children?: ReactNode }) {
  const layout = useLayout();
  const { pathname } = useLocation();
  const { density } = useSettings();
  const [settings, setSettings] = useState(false);
  const [search, setSearch] = useState(false);
  const title = pageTitle(pathname);
  const offset = layout === "desktop" ? "pl-sidebar-w" : layout === "tablet" ? "pl-rail" : "";

  return (
    <div className="min-h-screen bg-page" data-layout={layout} data-density={density}>
      {layout === "desktop" && <Sidebar onSettings={() => setSettings(true)} />}
      {layout === "tablet" && <IconRail onSettings={() => setSettings(true)} />}
      <div className={offset}>
        <header className="sticky top-0 z-10 flex h-header-h items-center gap-3 border-b border-line bg-header px-4 tablet:px-6">
          <h1 className="min-w-0 truncate text-title font-semibold text-title">{title}</h1>
          <div className="ml-auto flex items-center gap-2">
            {layout !== "mobile" && (
              <div className="w-[260px]">
                <SearchInput />
              </div>
            )}
            <HeaderAsOf compact={layout === "mobile"} />
            {layout === "mobile" && (
              <>
                <button
                  type="button"
                  aria-label="Search"
                  onClick={() => setSearch(!search)}
                  className="arc-touch flex items-center justify-center text-secondary"
                >
                  <IconSearch />
                </button>
                <button
                  type="button"
                  aria-label="Settings"
                  onClick={() => setSettings(true)}
                  className="arc-touch flex items-center justify-center text-secondary"
                >
                  <IconSettings />
                </button>
              </>
            )}
            <ThemeToggle />
          </div>
        </header>
        {layout === "mobile" && search && (
          <div className="border-b border-line bg-header px-4 py-2">
            <SearchInput autoFocus onDone={() => setSearch(false)} />
          </div>
        )}
        <main
          className={`mx-auto max-w-[1600px] px-4 py-4 tablet:px-8 tablet:py-8 desktop:px-[48px] desktop:py-10 ${
            layout === "mobile" ? "pb-[calc(56px+16px+env(safe-area-inset-bottom))]" : ""
          }`}
        >
          {children ?? <Outlet />}
        </main>
      </div>
      {layout === "mobile" && <TabBar />}
      {settings && <SettingsDialog onClose={() => setSettings(false)} />}
    </div>
  );
}
