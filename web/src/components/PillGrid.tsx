import { Fragment, type ReactNode } from "react";

import { GRID_COLS, gridRows } from "../lib/universe";

/**
 * E14.10 (D67): a 4-column grid of equal-width cells at every width (`grid-cols-4`, no
 * breakpoint). An open item's panel is inserted as a full-width cell right after its grid row
 * (accordion: the caller keeps one key open), so the columns stay aligned above and below.
 */
export function PillGrid<T>({
  items,
  keyOf,
  renderPill,
  openKey,
  renderPanel,
  testid = "pill-grid",
}: {
  items: readonly T[];
  keyOf: (item: T) => string;
  renderPill: (item: T) => ReactNode;
  openKey?: string | null;
  renderPanel?: (item: T) => ReactNode;
  testid?: string;
}) {
  return (
    <div className="grid grid-cols-4 gap-1.5" data-testid={testid} data-cols={GRID_COLS}>
      {gridRows(items).map((row, ri) => {
        const open = openKey == null ? undefined : row.find((it) => keyOf(it) === openKey);
        return (
          <Fragment key={ri}>
            {row.map((it) => (
              <div key={keyOf(it)} className="min-w-0" data-testid="pill-cell">
                {renderPill(it)}
              </div>
            ))}
            {open !== undefined && renderPanel && <div className="col-span-4 min-w-0">{renderPanel(open)}</div>}
          </Fragment>
        );
      })}
    </div>
  );
}
