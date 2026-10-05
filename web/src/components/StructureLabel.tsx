import { directionView, structureLabel, type Direction } from "../lib/overview";

/**
 * `Debit Vertical · Bullish` (D50): Title Case structure label, then the deterministic
 * direction (from legs) coloured bullish / bearish / neutral. No direction -> label only.
 */
export function StructureLabel({ kind, direction }: { kind: string | null | undefined; direction?: Direction | null }) {
  const d = directionView(direction);
  return (
    <>
      {structureLabel(kind)}
      {d && (
        <>
          {" · "}
          <span className={d.className} data-testid="direction" data-direction={direction ?? undefined}>
            {d.label}
          </span>
        </>
      )}
    </>
  );
}

/** Plain-text form for titles, sort keys and accessible names. */
export function structureText(kind: string | null | undefined, direction?: Direction | null): string {
  const d = directionView(direction);
  return d ? `${structureLabel(kind)} · ${d.label}` : structureLabel(kind);
}
