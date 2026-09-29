export const LIFECYCLE = [
  "proposed",
  "gate",
  "approval",
  "execution",
  "filled",
  "open",
  "exit",
  "closed",
] as const;
export type Stage = (typeof LIFECYCLE)[number];

/**
 * Horizontal lifecycle stepper (§4): done = accent, failed = neg, pending = track.
 * *reached* is the last stage reached; *failedAt* marks a stage that failed (e.g. gate).
 */
export function StatusStepper({
  reached,
  failedAt,
  stages = LIFECYCLE,
  compact = false,
}: {
  reached: Stage;
  failedAt?: Stage;
  /** A leading subset of the lifecycle, e.g. proposed -> filled for a proposal row. */
  stages?: readonly Stage[];
  /** Dots only (labels as tooltips), for table cells. */
  compact?: boolean;
}) {
  const upto = stages.indexOf(failedAt ?? reached);
  if (compact) {
    return (
      <ol className="flex items-center" aria-label={`lifecycle: ${failedAt ? `${failedAt} failed` : reached}`}>
        {stages.map((s, i) => {
          const state = failedAt === s ? "failed" : i <= upto ? "done" : "pending";
          const color =
            state === "failed" ? "var(--neg)" : state === "done" ? "var(--accent)" : "var(--track)";
          return (
            <li key={s} className="flex items-center" data-state={state} title={s}>
              {i > 0 && (
                <span className="h-[2px] w-2.5" style={{ background: i <= upto ? "var(--accent)" : "var(--track)" }} />
              )}
              <span className="h-2 w-2 rounded-full" style={{ background: color }} />
            </li>
          );
        })}
      </ol>
    );
  }
  return (
    <div className="arc-scroll-x">
      <ol className="flex min-w-[560px] items-start">
        {stages.map((s, i) => {
          const state = failedAt === s ? "failed" : i <= upto ? "done" : "pending";
          const color =
            state === "failed" ? "var(--neg)" : state === "done" ? "var(--accent)" : "var(--track)";
          return (
            <li key={s} className="flex flex-1 flex-col items-center" data-state={state}>
              <div className="flex w-full items-center">
                <span className="h-[2px] flex-1" style={{ background: i === 0 ? "transparent" : color }} />
                <span className="h-3 w-3 shrink-0 rounded-full" style={{ background: color }} />
                <span
                  className="h-[2px] flex-1"
                  style={{
                    background:
                      i === stages.length - 1
                        ? "transparent"
                        : i < upto
                          ? "var(--accent)"
                          : "var(--track)",
                  }}
                />
              </div>
              <span
                className={`mt-1.5 text-micro capitalize ${
                  state === "pending" ? "text-muted" : state === "failed" ? "text-neg-text" : "text-primary"
                }`}
              >
                {s}
              </span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
