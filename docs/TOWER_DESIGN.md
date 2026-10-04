# Control tower v2 — design spec (D35)

Reference: Copilot Money (macOS, dark theme) — owner screenshots 2026-09-28. This file is the
single source for tokens, components and layout rules. Every E8.7x card renders against it;
do not restate tokens in card bodies or code comments, import them from `web/src/theme/tokens.css`.

## 1. Principles

- **Flat, dense, quiet.** No drop shadows. Surfaces are separated by 1 px hairlines and a
  two-step luminance ladder (page → card). One accent colour. Colour carries meaning only for
  P&L direction, status and the accent.
- **Numbers first.** Every card leads with one hero number, a change pill, and a one-line
  comparison ("vs $x in <prior period>"). Charts have no axes unless a tick adds information
  (then at most two Y ticks and endpoint X ticks).
- **Everything drills down.** Every ticker, structure, proposal, run id and persona label is a
  link into the Trades or Ops pages. Row click opens detail; nothing is a dead end.
- **Read-only.** The tower has no state-changing control. Approve / halt / config stay in Slack.
- **Mobile is a first-class layout, not a fallback.** See §6.

## 2. Tokens

Two themes via `[data-theme="dark"|"light"]` on `<html>`; default follows
`prefers-color-scheme`, a header toggle overrides and persists in `localStorage`.

| Token | Dark (sampled from reference) | Light |
|---|---|---|
| `--bg-page` | `#00080F` | `#F5F7FA` |
| `--bg-sidebar` | `#030D19` | `#FFFFFF` |
| `--bg-card` | `#02101C` | `#FFFFFF` |
| `--bg-header` | `#041322` | `#FFFFFF` |
| `--bg-control` | `#041A2E` | `#EEF2F7` |
| `--bg-hover` | `#0C2135` | `#E9EEF5` |
| `--border` | `#112943` | `#E3E8EF` |
| `--border-input` | `#2A5D92` | `#B9C7DA` |
| `--track` | `#112943` | `#E3E8EF` |
| `--text-primary` | `#D0DBE5` (titles `#FFFFFF`) | `#0B1524` |
| `--text-secondary` | `#7089AA` | `#4A5A70` |
| `--text-muted` | `#567799` | `#7A8797` |
| `--accent` | `#6AA7F8` | `#2F7CF6` |
| `--accent-bar` | `#3787F7` | `#2F7CF6` |
| `--accent-slate` | `#365C8B` | `#A9BBD3` |
| `--range-active` | `#2A3A5C` | `#DCE6F5` |
| `--pos` / `--pos-text` / `--pos-bg` | `#0BBB00` / `#41AF24` / `#103621` | `#1A9E2C` / `#177F24` / `#E3F5E6` |
| `--neg` / `--neg-text` / `--neg-bg` | `#EE0000` / `#E52329` / `#3A1821` | `#D9232B` / `#B8161D` / `#FBE5E6` |
| `--warn` | `#FF7F02` | `#E56F00` |
| series extras | `#A34FF6` `#E0AC25` `#EA1BC6` `#49CCCB` `#B9C0CC` | same |

Shape and spacing (both themes):

```
--r-card: 12px; --r-control: 8px; --r-pill: 6px; --r-full: 999px; --r-label: 4px;
--s-1: 4px; --s-2: 8px; --s-3: 12px; --s-4: 16px; --s-6: 24px; --s-8: 32px; --s-10: 40px;
--sidebar-w: 260px; --header-h: 46px; --bar-h: 4px; --line-w: 2.5px;
--font: Inter, "Nunito Sans", -apple-system, system-ui, sans-serif;   /* tabular numerals on */
--fs-hero: 28px; --fs-stat: 22px; --fs-title: 16px; --fs-body: 14px; --fs-caption: 12px; --fs-micro: 11px;
```

Card: `bg-card`, 1 px `border`, `r-card`, padding 24 (16 on mobile), no shadow. Title 16 px
semibold top-left; action link top-right 12 px uppercase, 0.06 em tracking, `text-muted`, with
`↗` glyph (`TRADES ↗`, `VIEW ALL ↗`).

## 3. Number formatting (`web/src/lib/format.ts`, unit-tested)

- Money: thousands commas, `$` glyph rendered smaller and raised (`<sup>`-like span). Cents on
  prices, fills, P&L; whole dollars on equity, max loss, allocation. Negative money uses a
  leading `-` (`-$7,260.02`), never parentheses.
- Axis ticks: `$71K`, `-$9K`, `$1.2M`.
- Percent: 2 decimals below 100 %, 1 decimal at/above (`↗ 148.1%`).
- Change pill: `↗`/`↘` glyph + value, **no sign**; direction from the glyph, favourability from
  colour. Green = favourable, red = unfavourable. Rule table:
  - P&L, equity, PoP, net EV, win rate: up = green.
  - Cost, slippage, spend, drawdown, max loss, gate violations, latency, LLM cost: up = red.
  - Neutral quantities (contracts, count, delta exposure): no colour, `text-secondary`.
- Total return on a detail panel: explicit sign and parenthesised percent, neutral colour
  (`-$25,599.00 (-59.79%)`).
- Times: ET, `Mon 09-28 15:35`; ages `4m ago`, `2h ago`, `3d ago`. Every data card carries
  one freshness badge on its header line (§10): compact `● 3m ago`, with
  `as of <time> ET · <source> · stale after <3× cadence>` as its tooltip; when age > 3× the
  producing job's cadence the badge turns `--warn` and reads `stale · <age>`.
- Options: legs as `AMD 10/02 620C` (root, M/DD, strike, C/P); OCC symbol in `text-muted`
  10 px bold beneath or on hover.

## 4. Components (`web/src/components/`, one file each, both themes, all with a story in
`/kitchen-sink`)

| Component | Notes |
|---|---|
| `Shell` | sidebar (desktop) / bottom tab bar (mobile), header with page title, theme toggle, `AsOfBadge`, global search (ticker / hash / run id → jumps to detail) |
| `StatCard` | hero number, optional `ChangePill`, comparison line, optional child chart; `freshness` / `subtitle` / `headerExtra` as `Card` (§10) |
| `ChangePill` | see §3 |
| `RangeControl` | segmented `1D 1W 1M 3M YTD 1Y ALL`; a `SegmentedControl` preset; URL-synced (`?range=`) |
| `SegmentedControl` | one active `range-active` pill (white bold text); `sm` fits a card header; horizontal scroll on overflow (`data-scroll-x`); URL-synced key configurable (`param`) or controlled; 0.96 press scale; 44 px tall on touch (§10) |
| `Freshness` | the header freshness badge (§10): `● 3m ago` / `● stale · 1d ago` (`--warn`) / `● no data`; full line in a tap-to-reveal tooltip |
| `InfoTip` | ⓘ (1.5 px stroke, `currentColor`); popover on hover, focus and tap; Esc / outside tap closes; ≤ 2 short sentences + optional mono formula line; 44 px hit area on touch, 40 px desktop (§10) |
| `CappedList` | renders the first `limit` (default 8) items and `Show n more` / `Show less` (§10) |
| `TrendChart` | axis-less line, `line-w`, series colour by sign of the change, gradient fill to transparent (~30 % → 0), hollow-ring endpoint, hover tooltip (time + value), optional dotted reference line (prev close / start of range) |
| `DivergingBars` | monthly/daily bars, green above 0 red below, dotted gridlines, 2 Y ticks, "Now" marker (white 4 px-radius label + thin vertical line), future slots rendered empty so the axis is always full; optional dashed overlay lines (cumulative series) on their own hidden axis |
| `StackedBars` | category-stacked, 1 px gaps, negative segments allowed, no legend (tooltip) |
| `EquityDrawdownChart` | equity line (TrendChart styling) with the drawdown (distance below the running peak) shaded red in a lower strip (Performance) |
| `ModelScatter` | one dot per trade, x = modelled, y = realised, dotted y = x diagonal; dots on/above it `pos`, below `neg`; click → trade detail (Performance) |
| `ProportionBar` | dual/multi-segment 4 px bar + legend row (dot · label · value) |
| `ProgressRow` | dot · label · value · 4 px bar · right value (used for Greeks vs caps, allocation, budgets) |
| `Sparkline` | ~80×28, gradient fill, dotted reference line |
| `DataTable` | TanStack Table; sortable, column visibility, sticky header, row click → detail; density 40 px rows; on mobile renders `CardRow`s (see §6) |
| `KeyValueList` | 34 px rows, label `text-secondary` left, bold value right |
| `Timeline` | vertical stepper for the decision trail: persona label pill, stage, reason code, time, expandable body |
| `StatusStepper` | horizontal: proposed → gate → approval → execution → filled → open → exit → closed; done = accent, failed = neg, pending = track |
| `Section` | collapsible header `▶/▼ Title`, right-side sort/filter control; remembers open/closed per page + section in `localStorage` (`arc.section:<page>:<section>`), `defaultOpen` applies on first visit only (§10) |
| `FilterBar` | chips + dropdowns + date-range preset, URL-synced, "Clear" link, collapses into a "Filters (n)" sheet on mobile |
| `EmptyState` | glowing accent circle + caption |
| `Tile` | ~100×138 mover tile: ticker, name ellipsised, `Sparkline`, `ChangePill`; horizontal scroll container |
| `DetailPanel` | right-side panel on ≥1280 px, full page route below that |

Charts: Recharts, wrapped so no page imports Recharts directly. Tooltips share one style
(card surface, hairline border, caption text).

## 5. Layout (desktop)

- Sidebar `sidebar-w`, nav rows 30 px, active item = full-width accent pill (8 px radius).
  Nav: Overview · Trades · Positions · Performance · Ops. Below: `ACCOUNT` micro-header with
  paper account name, equity, and buying power as sidebar rows (dot · name · value).
  Footer: `Docs`, `Settings` (theme, density, refresh interval — all client-side).
- Content padding 40–60; two equal columns on Overview; one full-width + two halves on
  Performance; three columns (list · detail) on Trades ≥1280 px.
- Row gap 40, card gutter 40.

## 6. Mobile (≤768 px) and tablet (769–1279 px)

- Sidebar becomes a 6-item bottom tab bar (56 px, safe-area aware; Experiments added by E10.5); header keeps title +
  as-of badge + theme toggle. Search moves to a header icon.
- All grids collapse to one column; card padding 16; hero numbers `fs-stat`.
- `DataTable` renders each row as a `CardRow`: primary line (ticker · structure · change
  pill), secondary line (the three most important columns), tap → detail route.
  Column picker hidden; sort in the `FilterBar` sheet.
- `DetailPanel` is a full-page route with a back button.
- Charts stay full-width; `RangeControl` scrolls horizontally if needed; touch targets ≥ 44 px.
- Tablet: two columns, sidebar collapsed to icons (56 px) with tooltips.
- Verified with Playwright at 390×844, 768×1024 and 1440×900 in both themes (screenshots
  attached to the PR).

## 7. Data freshness and polling

- TanStack Query, `refetchInterval` 60 s (configurable in Settings: 30/60/120 s), paused when
  the tab is hidden, resumed on focus. No websockets; the backend is a read-only SQLite read.
- Every response carries `as_of` and each section carries the timestamp of the row it came
  from; the UI never displays a number without an age.
- Stale thresholds come from `config/routines.yaml` cadences (via `/api/meta`), not constants.

## 8. Semantics specific to Arc

- Equity trend colour = sign of change over the selected range.
- "Day P&L" = intraday equity − `last_equity` (broker's prior close) from the latest monitor
  heartbeat; falls back to the reconciled `pnl_snapshots` day P&L with a "reconciled" tag.
- Position P&L is per structure (sum of its legs' broker unrealised P&L), never per leg on
  the Overview. Legs appear in the drill-down.
- Gate FAIL, halt ACTIVE, reconcile MISMATCH, stale marks, and open ops alerts are the only
  things that may use `--neg`/`--warn` outside P&L.

## 9. Ops & pipeline (`/ops`, E8.7d)

- Loaders in `arc/tower/data_ops.py`, routes `GET /api/ops/{session,health,alerts,halts,
  runs,runs/{run_id},budget,context,context/{entry_id},sources,llm,config}`.
- Session timeline slots are `config/routines.yaml` slots (`arc.routines.schedule
  .slots_between`, 05:00–22:00 ET) joined to root `routine_runs` on `(job, scheduled_for)`.
  Colours: done `--accent`, running pulsing `--accent`, failed `--neg`, skipped / no_change
  muted, missed `--warn`, future `--track`. The D31 trading loop (`loop.job`) is its own row.
- Run detail renders the stored D27 manifest (`run_manifests.payload`) and the trace from
  `arc.context.trace.trace_runs`, the same serializer `arc context trace` prints. A read or
  write of a kind outside the job's declared contract is highlighted `--neg`.
- Order budget: the Ops widget was removed (D48, E8.8d); `GET /api/ops/budget` stays as a
  read-only API (the Overview's `Orders today n/200` comes from the snapshot).
- Effective config goes through `arc.control.service.ControlService` reads (`show`,
  `history`, `version`); a store without the D26 tables shows yaml values with a note.
- Health thresholds: tick stale after `monitoring.tick_stale_after`; the health check
  (LaunchAgent every 30 min) stale after 3×; the log judged against `monitoring.log.max_bytes`.

## 10. E8.8 visual pass (D48)

Mobile first: the owner reads the tower on an iPhone in Safari at 75 % page zoom, about
**520 CSS px** wide. Design at 520, then check 390 and 1440; if the two would need different
layouts, mobile wins. Same level of detail everywhere: numbers move (tooltip, tab, disclosure),
they are never dropped.

- **Title Case widget titles** (`P&L Today`, `Greeks vs Caps`, `Win / Loss`, `Today's
  Proposals`). Small words stay lower case inside a title (a, an, and, as, at, by, for, from,
  in, of, on, or, per, the, to, vs, via); acronyms and mixed-case tokens stay as written (P&L,
  LLM, EV, PoP, t0). `titleCase()` / `isTitleCase()` in `lib/format.ts`;
  `lib/sources.e88.test.ts` scans every `Card` / `StatCard` / `Section` / `SectionBlock` title
  literal and fails on sentence case. Slack cards stay sentence case (D22).
- **One freshness slot per widget**, on the header line right after the title (before the
  action link): `Card` / `StatCard` `freshness={{ at, cadenceS, label }}`. Compact text is
  `● 3m ago`, `● stale · 1d ago` (`--warn`, past 3× the cadence) or `● no data`; the full line
  `as of Fri 10-02 15:59 ET · monitor mark · stale after 15m` is the tooltip (tap-to-reveal on
  touch). A widget mixing two sources badges the **older** (`olderSource()`); per-value dots
  stay inline. The footer `asOf` slot is gone (a test greps `web/src` for `asOf=`);
  non-time footnotes (`last 24 h`, `local count`) go in the one-line `subtitle` under the title.
- **InfoTip rule:** on-card sub-text is at most one line (~60 chars at 520 px). Anything
  longer goes in an `InfoTip`: ≤ 2 short sentences plus an optional mono formula line.
- **Long lists are capped**: `CappedList` renders 8 rows and `Show n more`; never an
  unbounded wall on a phone.
- **Section persistence:** open/closed is remembered per page + section; `defaultOpen`
  applies only on the first visit.
- **Polish** (`make-interfaces-feel-better`): `tabular-nums` on every number (body and form
  controls), `text-wrap: balance` on headings / `pretty` on paragraphs, antialiased root,
  never `transition: all`, 0.96 press scale (`.arc-press`), hit areas ≥ 44 px on touch / 40 px
  desktop (`.arc-hit` pseudo-element), concentric radii (a tile inside a card uses
  `--r-control`).
- **phone-75 target:** the Playwright project `phone-75` (520×1125, `isMobile`, `hasTouch`,
  iPhone Safari UA, Chromium) runs every spec's `@phone-75`-tagged viewport block in both
  themes. `web/e2e/mobile.ts` holds the shared asserts later cards call per page:
  `expectNoOverflow` (page and every `.arc-card`, except `data-scroll-x` containers),
  `expectTouchTargets` (≥ 44 px) and `expectMinFontSize` (≥ `--fs-micro`, 11 px).

### E8.8d Ops pass

- **Widget order** (`OPS_WIDGETS` in `lib/ops.ts`, same on phone and desktop): Session
  Timeline · Sources · Health · LLM Usage · Context Store · Auto-Approve · Alerts · Halts ·
  Runs (the last three collapsible, closed on a first visit) · Config (a link to the full page
  `/ops/config`, effective config + change log). No Order Budget widget.
- **Timeline bands come from `config/routines.yaml`**, never code: each job may carry `label`,
  `group` (`sources | scout | trading_loop | position_management | post_market | other`),
  `persona` (chip; sources declare none) and `about` (one line ≤ 160 chars, the ⓘ text).
  A typo fails config load. Sources band by their D47 category (`sources.<category>`, labels
  from the category registry, so a rename needs no UI change); anything else falls under
  Other. The band header rolls up settled slots (`14/16 ok · 1 missed`).
- **≤ 768 px** the Gantt becomes a grouped list: band header → job rows (label, persona chip,
  `9/12 done · 1 missed · next 10:30`, ⓘ) with a dot strip that scrolls inside the row; tap a
  row for per-slot details. A band starts open only when it has a failed or missed slot.
- **Sources** group by D47 category: share, `max_age`, newest doc, worst-wins status
  (`ok < idle < pending < backoff < late < failed`). Category and in-category shares are read
  from `SourceRegistry`, never recomputed. Rows add `skipped (stale)` (E4.7) and, for YouTube
  channels, the E4.6 brief state (brief ok / pending: reason / no video in window). On a phone a
  category shows its header only unless its status is pending or worse.
- **Health** is one chip row (`Tick ok 1m`, `Log 0.2 / 50 MB`); the long text is in each chip's
  ⓘ, and a degraded or failed chip expands inline with its message.
- **Auto-Approve** is a key/value list (paper, live, scorecard gate, last flip) plus the gate
  line; **Alerts / Halts** collapse repeats like the Overview (`missed_window ×12`).
