import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

/**
 * Client-side settings (TOWER_DESIGN §5 footer): theme, density, refresh interval.
 * Persisted in localStorage; nothing is sent to the server (the tower is read-only).
 */

export type Theme = "light" | "dark";
export type ThemeChoice = Theme | "system";
export type Density = "comfortable" | "compact";
export const REFRESH_CHOICES = [30, 60, 120] as const;
export type RefreshS = (typeof REFRESH_CHOICES)[number];

const KEYS = { theme: "arc.theme", density: "arc.density", refresh: "arc.refresh" } as const;

function read(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string | null): void {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    /* private mode: settings last for the session only */
  }
}

function systemTheme(): Theme {
  return typeof window !== "undefined" && window.matchMedia?.("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

export function parseThemeChoice(v: string | null): ThemeChoice {
  return v === "light" || v === "dark" ? v : "system";
}

export function parseRefresh(v: string | null, fallback: RefreshS = 60): RefreshS {
  const n = Number(v);
  return (REFRESH_CHOICES as readonly number[]).includes(n) ? (n as RefreshS) : fallback;
}

interface SettingsValue {
  themeChoice: ThemeChoice;
  theme: Theme;
  setThemeChoice: (t: ThemeChoice) => void;
  toggleTheme: () => void;
  density: Density;
  setDensity: (d: Density) => void;
  refreshS: RefreshS;
  setRefreshS: (s: RefreshS) => void;
}

const SettingsContext = createContext<SettingsValue | null>(null);

export function SettingsProvider({ children }: { children: ReactNode }) {
  const [themeChoice, setChoice] = useState<ThemeChoice>(() => parseThemeChoice(read(KEYS.theme)));
  const [system, setSystem] = useState<Theme>(systemTheme);
  const [density, setDensityState] = useState<Density>(() =>
    read(KEYS.density) === "compact" ? "compact" : "comfortable",
  );
  const [refreshS, setRefresh] = useState<RefreshS>(() => parseRefresh(read(KEYS.refresh)));

  useEffect(() => {
    const mq = window.matchMedia?.("(prefers-color-scheme: dark)");
    if (!mq) return;
    const on = () => setSystem(mq.matches ? "dark" : "light");
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);

  const theme: Theme = themeChoice === "system" ? system : themeChoice;
  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
  }, [theme]);

  const setThemeChoice = useCallback((t: ThemeChoice) => {
    setChoice(t);
    write(KEYS.theme, t === "system" ? null : t);
  }, []);
  const toggleTheme = useCallback(() => {
    setThemeChoice(theme === "dark" ? "light" : "dark");
  }, [theme, setThemeChoice]);
  const setDensity = useCallback((d: Density) => {
    setDensityState(d);
    write(KEYS.density, d);
  }, []);
  const setRefreshS = useCallback((s: RefreshS) => {
    setRefresh(s);
    write(KEYS.refresh, String(s));
  }, []);

  const value = useMemo(
    () => ({
      themeChoice,
      theme,
      setThemeChoice,
      toggleTheme,
      density,
      setDensity,
      refreshS,
      setRefreshS,
    }),
    [themeChoice, theme, setThemeChoice, toggleTheme, density, setDensity, refreshS, setRefreshS],
  );
  return <SettingsContext.Provider value={value}>{children}</SettingsContext.Provider>;
}

export function useSettings(): SettingsValue {
  const ctx = useContext(SettingsContext);
  if (!ctx) throw new Error("useSettings outside SettingsProvider");
  return ctx;
}
