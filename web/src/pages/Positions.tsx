import { useSearchParams } from "react-router-dom";

import { AsOfBadge } from "../components/AsOfBadge";
import { Card } from "../components/Card";
import { EmptyState } from "../components/EmptyState";
import { useMeta, usePositions } from "../lib/useApi";
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
  return (
    <Card
      title="Positions"
      asOf={<AsOfBadge at={q.data?.marks_at} cadenceS={monitorS} label="monitor mark" />}
    >
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
        <PositionsTable rows={rows} full />
      )}
    </Card>
  );
}
