import { useSearchParams } from "react-router-dom";

import { Card } from "../components/Card";
import { EmptyState } from "../components/EmptyState";
import { formatEt } from "../lib/format";
import {
  exitCaseText,
  exitPathRows,
  exitPathStripText,
  exitPathVisible,
  exitVerdictChip,
  exitWatchChip,
  mandatoryLabel,
  type ExitPathStrip,
  type PositionRow,
} from "../lib/overview";
import { useMeta, usePositions } from "../lib/useApi";
import { Pill } from "./opsShared";
import { PositionsTable } from "./PositionsTable";

const STATUSES = ["open", "closed", "all"] as const;
type Status = (typeof STATUSES)[number];

/** Positions page (E8.7a): the Overview table with every column and an open/closed toggle. */
export function PositionsPage() {
  const [params, setParams] = useSearchParams();
  const raw = params.get("status");
  const status: Status = (STATUSES as readonly string[]).includes(raw ?? "") ? (raw as Status) : "open";
  const q = usePositions(status);
  const monitorS = useMeta().data?.cadences.monitor?.every_s;
  const rows = q.data?.items ?? [];
  const strip = q.data?.exit_path;
  const exits = exitPathVisible(strip);
  return (
    <Card title="Positions" freshness={{ at: q.data?.marks_at, cadenceS: monitorS, label: "monitor mark" }}>
      <div role="tablist" aria-label="Status" className="mb-3 inline-flex gap-1 rounded-control bg-control p-1">
        {STATUSES.map((s) => (
          <button
            key={s}
            type="button"
            role="tab"
            aria-selected={s === status}
            onClick={() => {
              const next = new URLSearchParams(params);
              next.set("status", s);
              setParams(next, { replace: true });
            }}
            className={`min-h-[32px] rounded-pill px-3 text-caption capitalize max-tablet:min-h-[44px] ${
              s === status ? "bg-range-active font-bold text-[color:var(--range-active-text)]" : "text-secondary hover:bg-hover"
            }`}
          >
            {s}
          </button>
        ))}
      </div>
      {q.isError ? (
        <EmptyState caption={`Could not load positions: ${String(q.error)}`} />
      ) : rows.length === 0 ? (
        <EmptyState caption={q.isLoading ? "Loading…" : `No ${status === "all" ? "" : `${status} `}structures.`} />
      ) : (
        <>
          {exits && strip && <ExitStrip s={strip} />}
          <PositionsTable rows={rows} full exits={exits} />
          {exits && <ExitDetails rows={exitPathRows(rows)} />}
        </>
      )}
    </Card>
  );
}

/** E13.14 (D56): the Exit path strip (mandatory signals pending · cases · closes · holds). */
function ExitStrip({ s }: { s: ExitPathStrip }) {
  return (
    <p
      data-testid="exit-path-strip"
      className="mb-3 rounded-control bg-control px-3 py-2 text-caption text-secondary tabular-nums [overflow-wrap:anywhere]"
    >
      <span className="font-semibold text-title">Exit path</span> · {exitPathStripText(s)}
    </p>
  );
}

/** Per position: the watch evidence, the case triggers and the Risk reason (read-only). */
function ExitDetails({ rows }: { rows: PositionRow[] }) {
  if (!rows.length) return null;
  return (
    <details className="mt-3" data-testid="exit-details">
      <summary className="flex min-h-[32px] cursor-pointer items-center text-caption font-semibold text-secondary max-tablet:min-h-[44px]">
        Exit path details ({rows.length})
      </summary>
      <ul className="divide-y divide-line">
        {rows.map((p) => {
          const w = exitWatchChip(p);
          const v = exitVerdictChip(p);
          const m = mandatoryLabel(p.mandatory_signal);
          return (
            <li key={p.id} className="grid gap-1 py-2 text-caption" data-testid={`exit-detail-${p.ticker}`}>
              <p className="flex flex-wrap items-center gap-1.5">
                <span className="font-semibold text-title">{p.ticker}</span>
                {w && <Pill tone={w.tone}>{w.text}</Pill>}
                {v && <Pill tone={v.tone}>Risk {v.text}</Pill>}
                {m && <Pill tone="neg">{m}</Pill>}
              </p>
              {p.exit_watch && (
                <div className="text-secondary">
                  <span className="font-semibold">Research:</span> {p.exit_watch.reason}
                  {(p.exit_watch.evidence ?? []).length > 0 && (
                    <ul className="list-disc pl-5">
                      {(p.exit_watch.evidence ?? []).map((e) => (
                        <li key={e} className="[overflow-wrap:anywhere]">{e}</li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
              {p.exit_case && (
                <div className="text-secondary tabular-nums">
                  <span className="font-semibold">Quant:</span> {exitCaseText(p)} · close now ${p.exit_case.close_now_net.toFixed(2)} ·
                  stop {p.exit_case.stop_state.replace(/_/g, " ")}
                  {p.exit_case.swap_ticker ? ` · swap → ${p.exit_case.swap_ticker}` : ""}
                  <ul className="list-disc pl-5">
                    {p.exit_case.triggers.map((t) => (
                      <li key={t} className="[overflow-wrap:anywhere]">{t}</li>
                    ))}
                  </ul>
                </div>
              )}
              {p.exit_review && (
                <p className="text-secondary">
                  <span className="font-semibold">Risk:</span> {p.exit_review.reason}{" "}
                  <span className="text-muted">({p.exit_review.reason_code.replace(/_/g, " ")} · {formatEt(p.exit_review.as_of)})</span>
                </p>
              )}
            </li>
          );
        })}
      </ul>
    </details>
  );
}
