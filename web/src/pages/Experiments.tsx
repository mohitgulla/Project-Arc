import type { ColumnDef } from "@tanstack/react-table";
import { useNavigate, useParams } from "react-router-dom";

import { Card } from "../components/Card";
import { DataTable } from "../components/DataTable";
import { DetailPanel } from "../components/DetailPanel";
import { EmptyState } from "../components/EmptyState";
import { ArmEquityChart, CumulativeDiffChart } from "../components/ExperimentCharts";
import { KeyValueList } from "../components/KeyValueList";
import { Section } from "../components/Section";
import {
  armRows,
  BREAKDOWN_LABEL,
  breakdownView,
  ciText,
  ciTone,
  cumulativeView,
  curveView,
  deltaText,
  pText,
  secondaryText,
  sessionsText,
  sortinoText,
  statusText,
  type ExperimentDetail,
  type ExperimentRow,
  type Tone,
} from "../lib/experiments";
import { formatEt, formatMoney } from "../lib/format";
import { useExperiment, useExperiments } from "../lib/useApi";

const TONE_TEXT: Record<Tone, string> = { pos: "text-pos-text", neg: "text-neg-text", neutral: "" };

const COLS: ColumnDef<ExperimentRow>[] = [
  { header: "Id", accessorKey: "experiment_id" },
  { header: "Status", id: "status", accessorFn: (r) => statusText(r) },
  { header: "Area", id: "area", accessorFn: (r) => `${r.area}${r.kind === "aa" ? " (A/A)" : ""}` },
  { header: "Sessions", id: "sessions", accessorFn: (r) => sessionsText(r) },
  {
    header: "Paired Daily P&L ∆",
    id: "primary",
    accessorFn: (r) => r.primary_mean ?? null,
    cell: ({ row }) => (
      <span className={TONE_TEXT[ciTone(row.original)]}>
        {deltaText(row.original)} (p {pText(row.original.primary_p)})
      </span>
    ),
  },
  { header: "Sortino ∆", id: "secondary", accessorFn: (r) => sortinoText(r) },
  { header: "Verdict", id: "verdict", accessorFn: (r) => r.verdict ?? "—" },
];

/** Experiments page (E10.5, D44): list + detail (right panel ≥1280 px, full page below). */
export function ExperimentsPage() {
  const { experimentId } = useParams();
  const navigate = useNavigate();
  const q = useExperiments();
  const rows = q.data?.items ?? [];
  return (
    <>
      <Card title="Experiments">
        {q.isError ? (
          <EmptyState caption={`Could not load experiments: ${String(q.error)}`} />
        ) : rows.length === 0 ? (
          <EmptyState caption={q.isLoading ? "Loading…" : "No experiments yet."} />
        ) : (
          <DataTable
            data={rows}
            columns={COLS}
            getRowId={(r) => r.experiment_id}
            onRowClick={(r) => navigate(`/experiments/${r.experiment_id}`)}
            cardRow={{
              primary: (r) => (
                <span>
                  {r.experiment_id} · {r.area} · {statusText(r)}
                </span>
              ),
              secondary: (r) => (
                <span>
                  {sessionsText(r)} · <span className={TONE_TEXT[ciTone(r)]}>{deltaText(r)} {ciText(r)}</span> ·{" "}
                  {secondaryText(r)}
                </span>
              ),
            }}
          />
        )}
      </Card>
      {experimentId && (
        <DetailPanel title={experimentId} onClose={() => navigate("/experiments")}>
          <ExperimentDetailView id={experimentId} />
        </DetailPanel>
      )}
    </>
  );
}

export function ExperimentDetailView({ id }: { id: string }) {
  const q = useExperiment(id);
  if (q.isError) return <EmptyState caption={`Could not load ${id}: ${String(q.error)}`} />;
  if (!q.data) return <EmptyState caption="Loading…" />;
  return <ExperimentBody d={q.data} />;
}

export function ExperimentBody({ d }: { d: ExperimentDetail }) {
  const e = d.experiment;
  const r = d.report;
  const arms = armRows(r);
  const breakdowns = breakdownView(r);
  return (
    <div className="flex flex-col gap-4" data-testid="experiment-detail">
      <p className="text-secondary">{e.title}</p>
      {e.line && (
        <p className="text-caption text-muted" data-testid="experiment-line">
          {e.line}
        </p>
      )}
      <KeyValueList
        items={[
          { label: "Status", value: statusText(e) },
          { label: "Sessions", value: sessionsText(e) },
          {
            label: "Paired Daily P&L",
            value: (
              <span className={TONE_TEXT[ciTone(e)]}>
                ∆ {deltaText(e)} (p {pText(e.primary_p)}) {ciText(e)}
              </span>
            ),
            hint: e.ci_level ? `always-valid ${Math.round(e.ci_level * 100)}% CI, % of t0 equity` : undefined,
          },
          {
            label: "Sortino Ratio",
            value: sortinoText(e),
            hint:
              e.sortino_control !== null && e.sortino_control !== undefined
                ? `control ${e.sortino_control.toFixed(2)} · treatment ${e.sortino_treatment?.toFixed(2) ?? "n/a"}`
                : undefined,
          },
          { label: "Verdict", value: e.verdict ?? "—", hint: e.verdict_reason ?? undefined },
          { label: "Evaluated", value: e.evaluated_at ? formatEt(e.evaluated_at) : "not yet" },
        ]}
      />
      {r ? (
        <>
          <Section title="Equity from t0">
            <ArmEquityChart data={curveView(d)} />
          </Section>
          <Section title="Cumulative difference">
            <CumulativeDiffChart data={cumulativeView(d)} />
          </Section>
          <Section title="Arms">
            <table className="w-full text-body tabular-nums" data-testid="arm-table">
              <thead>
                <tr className="text-left text-caption text-muted">
                  <th className="py-1">Arm</th>
                  <th>P&amp;L</th>
                  <th>Max DD</th>
                  <th>Worst day</th>
                  <th>Orders</th>
                  <th>Fills</th>
                </tr>
              </thead>
              <tbody>
                {arms.map((a) => (
                  <tr key={a.arm} className="border-t border-line">
                    <td className="py-1">{a.arm}</td>
                    <td>{a.pnl}</td>
                    <td>{a.maxDrawdown}</td>
                    <td>{a.worstDay}</td>
                    <td>{a.orders}</td>
                    <td>{a.fills}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Section>
          <Section title="Breakdowns" defaultOpen={breakdowns.length > 0}>
            {breakdowns.length === 0 ? (
              <p className="text-caption text-muted">No closed trades in the window yet.</p>
            ) : (
              <table className="w-full text-body tabular-nums" data-testid="breakdown-table">
                <thead>
                  <tr className="text-left text-caption text-muted">
                    <th className="py-1">By</th>
                    <th>Key</th>
                    <th>Control</th>
                    <th>Treatment</th>
                  </tr>
                </thead>
                <tbody>
                  {breakdowns.map((b) => (
                    <tr key={`${b.by}|${b.key}`} className="border-t border-line">
                      <td className="py-1">{BREAKDOWN_LABEL[b.by] ?? b.by}</td>
                      <td>{b.key}</td>
                      <td>{b.control ? `${b.control.trades} · ${formatMoney(b.control.pnl, "pnl")}` : "—"}</td>
                      <td>{b.treatment ? `${b.treatment.trades} · ${formatMoney(b.treatment.pnl, "pnl")}` : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Section>
        </>
      ) : (
        <p className="text-caption text-muted">No evaluation yet: the spec and status are shown until the first EOD run.</p>
      )}
      <Section title="Spec and hashes" defaultOpen={false}>
        <KeyValueList
          items={[
            { label: "Spec hash", value: <code className="text-micro">{d.spec_hash.slice(0, 16)}</code> },
            {
              label: "Pre-registered hash",
              value: <code className="text-micro">{d.registered_hash ? d.registered_hash.slice(0, 16) : "not registered"}</code>,
            },
            { label: "Control sha", value: <code className="text-micro">{d.running?.control_sha?.slice(0, 12) ?? "—"}</code> },
            { label: "Treatment sha", value: <code className="text-micro">{d.report?.treatment_sha?.slice(0, 12) ?? "—"}</code> },
            { label: "Evaluator sha", value: <code className="text-micro">{d.report?.evaluator_sha?.slice(0, 12) ?? "—"}</code> },
            { label: "Report hash", value: <code className="text-micro">{d.report_hash?.slice(0, 16) ?? "—"}</code> },
            { label: "Revision", value: String(d.revision) },
          ]}
        />
        <pre className="mt-2 max-h-[320px] overflow-auto rounded-control bg-control p-2 text-micro" data-testid="experiment-spec">
          {JSON.stringify(d.spec, null, 2)}
        </pre>
      </Section>
    </div>
  );
}
