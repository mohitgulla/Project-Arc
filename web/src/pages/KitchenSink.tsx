import { CAT } from "../lib/personas.fixture";
import type { ColumnDef } from "@tanstack/react-table";
import type { ReactNode } from "react";

import { AsOfBadge } from "../components/AsOfBadge";
import { Card } from "../components/Card";
import { CappedList } from "../components/CappedList";
import { ChangePill } from "../components/ChangePill";
import { DataTable } from "../components/DataTable";
import { DetailPanel } from "../components/DetailPanel";
import { DivergingBars, type BarPoint } from "../components/DivergingBars";
import { EmptyState } from "../components/EmptyState";
import { FilterBar, type FilterDef } from "../components/FilterBar";
import { InfoTip } from "../components/InfoTip";
import { KeyValueList } from "../components/KeyValueList";
import { Money } from "../components/Money";
import { ProgressRow } from "../components/ProgressRow";
import { ProportionBar } from "../components/ProportionBar";
import { RangeControl } from "../components/RangeControl";
import { Section } from "../components/Section";
import { SearchInput, SettingsPanel, ThemeToggle } from "../components/Shell";
import { SegmentedControl } from "../components/SegmentedControl";
import { Sparkline } from "../components/Sparkline";
import { StackedBars } from "../components/StackedBars";
import { StatCard } from "../components/StatCard";
import { StatusStepper } from "../components/StatusStepper";
import { Tile, TileRow } from "../components/Tile";
import { Timeline } from "../components/Timeline";
import { TrendChart, type TrendPoint } from "../components/TrendChart";
import { formatEt, formatLeg, formatMoney, formatNumber, formatPercent, formatTotalReturn } from "../lib/format";

// ---------------------------------------------------------------------------
// Deterministic sample data (screenshots must be stable)
// ---------------------------------------------------------------------------

const NOW = Date.parse("2026-09-28T19:40:00Z"); // Mon 09-28 15:40 ET

function wave(n: number, base: number, amp: number, drift: number): number[] {
  return Array.from({ length: n }, (_, i) => base + drift * i + amp * Math.sin(i / 2.3) + (amp / 3) * Math.cos(i * 1.7));
}

const EQUITY: TrendPoint[] = wave(40, 100_000, 420, 34).map((v, i) => ({
  t: new Date(Date.parse("2026-08-18T20:00:00Z") + i * 86_400_000).toISOString(),
  v: Math.round(v * 100) / 100,
}));
const EQUITY_DOWN: TrendPoint[] = wave(30, 101_200, 260, -41).map((v, i) => ({
  t: new Date(Date.parse("2026-09-28T13:30:00Z") + i * 780_000).toISOString(),
  v,
}));

const MONTHS: BarPoint[] = [
  ["Jan", 1840],
  ["Feb", -620],
  ["Mar", 2410],
  ["Apr", 980],
  ["May", -1320],
  ["Jun", 1560],
  ["Jul", 3100],
  ["Aug", -410],
  ["Sep", 1275],
  ["Oct", null],
  ["Nov", null],
  ["Dec", null],
].map(([label, v]) => ({ label: label as string, v: v as number | null }));

const STACK_SERIES = [
  { key: "vertical_debit", label: "Debit verticals", color: "var(--series-1)" },
  { key: "long_call", label: "Long calls", color: "var(--series-2)" },
  { key: "long_put", label: "Long puts", color: "var(--series-4)" },
];
const STACK = ["W35", "W36", "W37", "W38", "W39", "W40"].map((label, i) => ({
  label,
  vertical_debit: [320, -140, 410, 120, 260, -80][i]!,
  long_call: [110, 90, -220, 340, -60, 180][i]!,
  long_put: [-40, 60, 80, -120, 150, 40][i]!,
}));

interface Row {
  id: string;
  ticker: string;
  structure: string;
  legs: string[];
  contracts: number;
  entry: number;
  pnl: number;
  pnlPct: number;
  opened: string;
}

const ROWS: Row[] = [
  {
    id: "s1",
    ticker: "AMD",
    structure: "Debit call vertical",
    legs: ["AMD261002C00620000", "AMD261002C00640000"],
    contracts: 3,
    entry: -6.4,
    pnl: 412.5,
    pnlPct: 0.2148,
    opened: "2026-09-22T14:05:00Z",
  },
  {
    id: "s2",
    ticker: "SPY",
    structure: "Debit put vertical",
    legs: ["SPY261030P00711000", "SPY261030P00700000"],
    contracts: 2,
    entry: -3.1,
    pnl: -186.0,
    pnlPct: -0.3,
    opened: "2026-09-24T15:40:00Z",
  },
  {
    id: "s3",
    ticker: "NVDA",
    structure: "Long call",
    legs: ["NVDA261016C00190000"],
    contracts: 1,
    entry: -7.25,
    pnl: 1062.0,
    pnlPct: 1.481,
    opened: "2026-09-10T13:45:00Z",
  },
];

const COLUMNS: ColumnDef<Row, unknown>[] = [
  { accessorKey: "ticker", header: "Ticker", cell: (c) => <span className="font-semibold text-title">{String(c.getValue())}</span> },
  { accessorKey: "structure", header: "Structure" },
  {
    id: "legs",
    header: "Legs",
    enableSorting: false,
    cell: (c) => (
      <span className="text-secondary" title={c.row.original.legs.join(" / ")}>
        {c.row.original.legs.map(formatLeg).join(" / ")}
      </span>
    ),
  },
  { accessorKey: "contracts", header: "Qty" },
  { accessorKey: "entry", header: "Entry", cell: (c) => <Money value={Number(c.getValue())} kind="price" /> },
  {
    accessorKey: "pnl",
    header: "P&L",
    cell: (c) => (
      <span className="inline-flex items-center gap-2">
        <Money value={Number(c.getValue())} kind="pnl" />
        <ChangePill value={c.row.original.pnlPct} metric="pnl" />
      </span>
    ),
  },
  { accessorKey: "opened", header: "Opened", cell: (c) => <span className="text-secondary">{formatEt(String(c.getValue()))}</span> },
];

const FILTERS: FilterDef[] = [
  {
    key: "stage",
    label: "Stage",
    type: "chips",
    options: [
      { value: "open", label: "Open" },
      { value: "closed", label: "Closed" },
      { value: "rejected", label: "Rejected" },
    ],
  },
  {
    key: "persona",
    label: "Persona",
    type: "select",
    // E13.14: real pages build these from /api/meta (lib/personas.ts); the sink uses the
    // test catalogue so the demo needs no server.
    options: CAT.personas.filter((p) => p.llm || p.key === "broker").map((p) => ({ value: p.key, label: p.label })),
  },
];

const GROUP_BY = [
  { value: "structure", label: "Structure" },
  { value: "ticker", label: "Ticker" },
  { value: "regime", label: "Regime at entry" },
  { value: "persona", label: "Persona" },
] as const;
const WINDOWS = ["1D", "1W", "1M", "3M", "YTD", "ALL"].map((value) => ({ value }));
const ACTIVITY = Array.from({ length: 12 }, (_, i) => `${String(15 - Math.floor(i / 2)).padStart(2, "0")}:${i % 2 ? "05" : "35"} · monitor tick ${12 - i}`);

// ---------------------------------------------------------------------------
// Stories: one per component in TOWER_DESIGN §4
// ---------------------------------------------------------------------------

function Story({ name, children }: { name: string; children: ReactNode }) {
  return (
    <div data-story={name} className="min-w-0">
      <div className="arc-micro-header mb-2">{name}</div>
      {children}
    </div>
  );
}

function Stories() {
  return (
    <div className="grid gap-8">
      <Story name="Shell (header pieces)">
        <Card>
          <div className="flex flex-wrap items-center gap-3">
            <div className="w-full max-w-[260px]">
              <SearchInput />
            </div>
            <AsOfBadge at="2026-09-28T19:36:00Z" cadenceS={300} now={NOW} />
            <AsOfBadge at="2026-09-28T17:40:00Z" cadenceS={300} now={NOW} />
            <AsOfBadge at={null} />
            <ThemeToggle />
          </div>
          <div className="mt-4">
            <SettingsPanel />
          </div>
        </Card>
      </Story>

      <Story name="StatCard + RangeControl + TrendChart">
        <StatCard
          title="Equity"
          action={{ label: "Performance", to: "/performance" }}
          value={<Money value={EQUITY[EQUITY.length - 1]!.v} kind="equity" />}
          change={<ChangePill value={0.0151} metric="equity" />}
          comparison={<>vs <Money value={EQUITY[0]!.v} kind="equity" /> on {formatEt(EQUITY[0]!.t).slice(4, 9)}</>}
          freshness={{ at: "2026-09-28T19:36:00Z", cadenceS: 300, label: "monitor mark", now: NOW }}
        >
          <RangeControl />
          <div className="mt-3">
            <TrendChart data={EQUITY} reference={EQUITY[0]!.v} />
          </div>
        </StatCard>
      </Story>

      <Story name="Freshness (fresh / stale / no data)">
        <div className="grid gap-3" data-testid="freshness-stories">
          <Card title="P&L Today" freshness={{ at: "2026-09-28T19:37:00Z", cadenceS: 300, label: "monitor mark", now: NOW }}>
            <p className="text-caption text-secondary">Fresh: within 3× the monitor cadence.</p>
          </Card>
          <Card
            title="Greeks vs Caps"
            freshness={{ at: "2026-09-27T19:40:00Z", cadenceS: 300, label: "monitor mark", now: NOW }}
            action={{ label: "VIEW ALL", to: "/positions" }}
          >
            <p className="text-caption text-secondary">Stale: older than 3× the cadence, in --warn.</p>
          </Card>
          <Card title="Movers" freshness={{ at: null, label: "monitor mark", now: NOW }} subtitle="last 24 h · subtitle line">
            <p className="text-caption text-secondary">No data: nothing produced yet.</p>
          </Card>
        </div>
      </Story>

      <Story name="InfoTip">
        <Card
          title="Net EV"
          headerExtra={
            <InfoTip label="About net EV" formula="EV − spread − slippage − fees" testid="infotip-story">
              Expected value under the managed exit policy, after all costs. Hold-to-expiry EV is on the trade.
            </InfoTip>
          }
        >
          <p className="text-caption text-secondary">
            Net EV <span className="font-semibold text-primary">+$87.25</span>{" "}
            <InfoTip label="About PoP">Probability the trade closes at a profit under the managed exits.</InfoTip>
          </p>
        </Card>
      </Story>

      <Story name="SegmentedControl">
        <Card title="Breakdowns" headerExtra={<SegmentedControl size="sm" label="Group by" options={GROUP_BY} fallback="structure" />}>
          <SegmentedControl label="Window" options={WINDOWS} fallback="3M" />
        </Card>
      </Story>

      <Story name="CappedList">
        <Card title="Recent Activity" subtitle="12 rows, capped at 8">
          <CappedList className="grid" testid="capped-story" noun="items">
            {ACTIVITY.map((a) => (
              <li key={a} className="border-b border-line py-2 text-caption last:border-b-0">
                {a}
              </li>
            ))}
          </CappedList>
        </Card>
      </Story>

      <Story name="TrendChart (down, dotted prev close)">
        <Card title="Day P&L">
          <div className="flex items-baseline gap-3">
            <span className="text-stat font-semibold text-title">
              <Money value={-1203.44} kind="pnl" />
            </span>
            <ChangePill value={-0.0119} metric="pnl" />
          </div>
          <TrendChart data={EQUITY_DOWN} reference={EQUITY_DOWN[0]!.v} height={140} />
        </Card>
      </Story>

      <Story name="ChangePill">
        <Card>
          <div className="flex flex-wrap items-center gap-2">
            <ChangePill value={1.481} metric="pnl" />
            <ChangePill value={-0.0512} metric="equity" />
            <ChangePill value={0.12} metric="slippage" />
            <ChangePill value={-0.3} metric="drawdown" />
            <ChangePill value={2} metric="contracts" format={(a) => formatNumber(a)} />
            <ChangePill value={-312} metric="pnl" format={(a) => formatMoney(a, "pnl")} />
            <ChangePill value={0} metric="pnl" />
          </div>
          <p className="mt-3 text-caption text-secondary">
            Total return (detail panel): <span className="font-semibold text-primary">{formatTotalReturn(-25599, -0.5979)}</span>
          </p>
        </Card>
      </Story>

      <Story name="DivergingBars">
        <Card title="Monthly P&L" action={{ label: "View all", to: "/performance" }}>
          <DivergingBars data={MONTHS} nowLabel="Sep" />
        </Card>
      </Story>

      <Story name="StackedBars">
        <Card title="P&L by Structure">
          <StackedBars data={STACK} series={STACK_SERIES} height={170} />
        </Card>
      </Story>

      <Story name="ProportionBar">
        <Card title="Allocation">
          <ProportionBar
            segments={[
              { label: "Options at risk", value: 4620, color: "var(--accent-bar)", display: <Money value={4620} kind="allocation" /> },
              { label: "Cash", value: 95880, color: "var(--accent-slate)", display: <Money value={95880} kind="allocation" /> },
            ]}
          />
        </Card>
      </Story>

      <Story name="ProgressRow">
        <Card title="Greeks vs Caps">
          <ProgressRow label="Net Δ" value="25.0" right="cap 301.5" fraction={25 / 301.5} />
          <ProgressRow label="Net ν ($/vol pt)" value={<Money value={-3} kind="pnl" />} right="cap $503" fraction={3 / 502.5} />
          <ProgressRow label="Orders" value="182" right="of 200" fraction={182 / 200} warnAt={0.875} />
          <ProgressRow label="Max loss / 5% cap" value={<Money value={5400} kind="max_loss" />} right="cap $5,025" fraction={5400 / 5025} />
        </Card>
      </Story>

      <Story name="Sparkline + Tile">
        <Card title="Movers">
          <TileRow>
            <Tile ticker="NVDA" name="NVIDIA Corporation" values={wave(20, 180, 4, 0.6)} change={0.0342} />
            <Tile ticker="AMD" name="Advanced Micro Devices, Inc." values={wave(20, 160, 3, 0.2)} change={0.0118} />
            <Tile ticker="SPY" name="SPDR S&P 500 ETF Trust" values={wave(20, 712, 2, -0.3)} change={-0.0064} />
            <Tile ticker="TSLA" name="Tesla, Inc." values={wave(20, 250, 6, -0.9)} change={-0.0412} />
            <Tile ticker="QQQ" name="Invesco QQQ Trust" values={wave(20, 520, 2, 0.1)} change={0.0021} />
          </TileRow>
          <div className="mt-3 flex items-center gap-3 text-caption text-secondary">
            Sparkline <Sparkline values={wave(24, 10, 1, 0.08)} />
            <Sparkline values={wave(24, 10, 1, -0.08)} />
          </div>
        </Card>
      </Story>

      <Story name="DataTable (CardRow on mobile)">
        <Card title="Open Positions" action={{ label: "Trades", to: "/trades" }}>
          <DataTable
            data={ROWS}
            columns={COLUMNS}
            getRowId={(r) => r.id}
            onRowClick={() => undefined}
            cardRow={{
              primary: (r) => (
                <>
                  {r.ticker} <span className="font-normal text-secondary">· {r.structure}</span>
                  <span className="ml-auto">
                    <ChangePill value={r.pnlPct} metric="pnl" />
                  </span>
                </>
              ),
              secondary: (r) => (
                <>
                  <span>{r.contracts}×</span>
                  <Money value={r.pnl} kind="pnl" />
                  <span>{formatEt(r.opened)}</span>
                </>
              ),
            }}
          />
        </Card>
      </Story>

      <Story name="KeyValueList">
        <Card title="Structure">
          <KeyValueList
            items={[
              { label: "Legs", value: formatLeg("AMD261002C00620000"), hint: "AMD261002C00620000" },
              { label: "Net debit", value: <Money value={6.4} kind="price" /> },
              { label: "Max loss", value: <Money value={1920} kind="max_loss" /> },
              { label: "PoP (managed)", value: formatPercent(0.4312) },
              { label: "Net EV (after costs)", value: <Money value={87.25} kind="pnl" /> },
            ]}
          />
        </Card>
      </Story>

      <Story name="Timeline">
        <Card title="Decision Trail">
          <Timeline
            items={[
              { id: "1", persona: "Scalp", stage: "Candidate", reason: "news_catalyst", at: "Mon 09-28 09:12" },
              { id: "2", persona: "Quant", stage: "Structure", reason: "ev_ranked", at: "Mon 09-28 09:34", body: "Debit call vertical 620/640, net EV $87.25 after costs." },
              { id: "3", persona: "Risk", stage: "Gate FAIL", reason: "delta_cap", at: "Mon 09-28 09:35", status: "failed", body: "post-trade |$Δ| $53,000.00 > cap $50,000.00" },
              { id: "4", persona: "Broker", stage: "Awaiting approval", at: "—", status: "pending" },
            ]}
          />
        </Card>
      </Story>

      <Story name="StatusStepper">
        <Card>
          <StatusStepper reached="open" />
          <div className="mt-4">
            <StatusStepper reached="gate" failedAt="gate" />
          </div>
        </Card>
      </Story>

      <Story name="Section + FilterBar">
        <Card>
          <Section title="Proposals" control={<span className="arc-action">Sort: newest</span>}>
            <FilterBar defs={FILTERS} />
          </Section>
          <Section title="Collapsed Section" defaultOpen={false}>
            <p>Hidden content</p>
          </Section>
        </Card>
      </Story>

      <Story name="EmptyState">
        <Card>
          <EmptyState caption="No open positions. New proposals appear after the 09:30 chain." />
        </Card>
      </Story>

      <Story name="DetailPanel">
        <div className="h-[260px]">
          <DetailPanel inline title="AMD · Debit call vertical" onClose={() => undefined}>
            <StatusStepper reached="open" />
            <p className="mt-3 text-caption text-secondary">
              Right-side panel on ≥1280px; full-page view with a back button below that.
            </p>
          </DetailPanel>
        </div>
      </Story>
    </div>
  );
}

/** `/kitchen-sink`: every TOWER_DESIGN §4 component with sample data, dark and light side by side. */
export function KitchenSink() {
  return (
    <div>
      <p className="mb-6 max-w-3xl text-caption text-secondary">
        Every component in TOWER_DESIGN §4 with sample data, in both themes. Sample numbers only: nothing
        here reads the audit store.
      </p>
      <div className="grid gap-6 desktop:grid-cols-2 desktop:gap-10">
        {(["dark", "light"] as const).map((t) => (
          <div
            key={t}
            data-theme={t}
            data-testid={`kitchen-${t}`}
            className="min-w-0 rounded-card border border-line p-3 tablet:p-5"
            style={{ background: "var(--bg-page)" }}
          >
            <h2 className="mb-4 text-title font-semibold text-title">{t === "dark" ? "Dark" : "Light"} theme</h2>
            <Stories />
          </div>
        ))}
      </div>
    </div>
  );
}
