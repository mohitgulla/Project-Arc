import type { Config } from "tailwindcss";

// Every colour, radius, size and font maps to a CSS variable from src/theme/tokens.css
// (docs/TOWER_DESIGN.md §2). Never hard-code a hex value in a component.
const v = (name: string) => `var(--${name})`;

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: ["selector", '[data-theme="dark"]'],
  theme: {
    screens: {
      // §6: mobile <=768, tablet 769-1279, desktop >=1280
      tablet: "769px",
      desktop: "1280px",
    },
    extend: {
      colors: {
        page: v("bg-page"),
        sidebar: v("bg-sidebar"),
        card: v("bg-card"),
        header: v("bg-header"),
        control: v("bg-control"),
        hover: v("bg-hover"),
        line: v("border"),
        "line-input": v("border-input"),
        track: v("track"),
        primary: v("text-primary"),
        title: v("text-title"),
        secondary: v("text-secondary"),
        muted: v("text-muted"),
        accent: v("accent"),
        "accent-bar": v("accent-bar"),
        "accent-slate": v("accent-slate"),
        "range-active": v("range-active"),
        pos: v("pos"),
        "pos-text": v("pos-text"),
        "pos-bg": v("pos-bg"),
        neg: v("neg"),
        "neg-text": v("neg-text"),
        "neg-bg": v("neg-bg"),
        warn: v("warn"),
        "series-1": v("series-1"),
        "series-2": v("series-2"),
        "series-3": v("series-3"),
        "series-4": v("series-4"),
        "series-5": v("series-5"),
      },
      borderRadius: {
        card: v("r-card"),
        control: v("r-control"),
        pill: v("r-pill"),
        full: v("r-full"),
        label: v("r-label"),
      },
      spacing: {
        "sidebar-w": v("sidebar-w"),
        "header-h": v("header-h"),
        rail: v("rail-w"),
        tabbar: v("tabbar-h"),
      },
      fontFamily: { sans: v("font") },
      fontSize: {
        hero: [v("fs-hero"), { lineHeight: "1.15", fontWeight: "600" }],
        stat: [v("fs-stat"), { lineHeight: "1.2", fontWeight: "600" }],
        title: [v("fs-title"), { lineHeight: "1.3", fontWeight: "600" }],
        body: [v("fs-body"), { lineHeight: "1.45" }],
        caption: [v("fs-caption"), { lineHeight: "1.4" }],
        micro: [v("fs-micro"), { lineHeight: "1.3" }],
      },
      height: { bar: v("bar-h") },
    },
  },
  plugins: [],
} satisfies Config;
