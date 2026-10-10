import type { ReactNode } from "react";
import { Link } from "react-router-dom";

/**
 * E14.10 (D67): one ticker in a 4-column pill grid. Fixed height (32 px, 44 px on touch),
 * ticker left and truncated, an optional muted micro score right, and small markers for
 * `carried` (hollow ring) and `also in` (filled dot); each marker's title explains it.
 * A button (Ops › Universe: toggles the detail panel) or a link (Today's Pick).
 */
export interface TickerPillProps {
  ticker: string;
  score?: string | null;
  carriedTitle?: string | null;
  alsoTitle?: string | null;
  /** Button mode: the detail panel is open. */
  open?: boolean;
  onToggle?: () => void;
  /** Link mode. */
  to?: string;
  /** Hover title (full detail lines). */
  title?: string;
  /** `aria-controls` target in button mode. */
  controls?: string;
  testid?: string;
}

const BASE =
  "arc-press flex h-8 w-full min-w-0 items-center gap-1 rounded-control border px-2 text-left text-caption max-tablet:h-11 " +
  "focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent";

export function TickerPill({ ticker, score, carriedTitle, alsoTitle, open, onToggle, to, title, controls, testid = "ticker-pill" }: TickerPillProps) {
  const tone = open ? "border-accent bg-hover" : "border-line bg-control hover:bg-hover";
  const body: ReactNode = (
    <>
      <span className="min-w-0 flex-1 truncate font-semibold text-primary" data-testid="pill-ticker">
        {ticker}
      </span>
      {carriedTitle && (
        <span
          className="inline-block size-1.5 shrink-0 rounded-full border border-secondary"
          title={carriedTitle}
          aria-label={carriedTitle}
          role="img"
          data-testid="pill-carried"
        />
      )}
      {alsoTitle && (
        <span
          className="inline-block size-1.5 shrink-0 rounded-full bg-secondary"
          title={alsoTitle}
          aria-label={alsoTitle}
          role="img"
          data-testid="pill-also"
        />
      )}
      {score && (
        <span className="shrink-0 text-micro tabular-nums text-muted" data-testid="pill-score">
          {score}
        </span>
      )}
    </>
  );
  if (to)
    return (
      <Link to={to} title={title} className={`${BASE} ${tone}`} data-testid={testid} data-ticker={ticker}>
        {body}
      </Link>
    );
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-expanded={!!open}
      aria-controls={controls}
      aria-label={`${ticker} details`}
      title={title}
      className={`${BASE} ${tone}`}
      data-testid={testid}
      data-ticker={ticker}
    >
      {body}
    </button>
  );
}
