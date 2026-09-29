import type { ReactNode } from "react";

/** Glowing accent circle + caption (§4). */
export function EmptyState({ caption, children }: { caption: ReactNode; children?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-4 py-10 text-center">
      <span
        className="h-12 w-12 rounded-full"
        style={{
          background: "radial-gradient(circle, var(--accent) 0%, var(--accent-slate) 70%)",
          boxShadow: "0 0 24px 4px color-mix(in srgb, var(--accent) 35%, transparent)",
          opacity: 0.85,
        }}
        aria-hidden="true"
      />
      <p className="max-w-xs text-caption text-secondary">{caption}</p>
      {children}
    </div>
  );
}
