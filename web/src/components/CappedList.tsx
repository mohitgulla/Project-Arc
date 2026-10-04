import { Children, useId, useState } from "react";
import type { ReactNode } from "react";

/** Default cap for long lists on a card (D48 mobile acceptance). */
export const LIST_CAP = 8;

/**
 * A list that renders its first `limit` items and a `Show n more` toggle (TOWER_DESIGN §10),
 * so a long list never becomes an unbounded wall on a phone. Children are the list items
 * (`<li>` for `ul`/`ol`); nothing is dropped, the rest is one tap away.
 */
export function CappedList({
  children,
  limit = LIST_CAP,
  as: Tag = "ul",
  className = "",
  testid,
  noun = "",
}: {
  children: ReactNode;
  limit?: number;
  as?: "ul" | "ol" | "div";
  className?: string;
  testid?: string;
  /** Optional plural noun for the button: `Show 4 more runs`. */
  noun?: string;
}) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const items = Children.toArray(children);
  const hidden = Math.max(0, items.length - limit);
  const shown = open || hidden === 0 ? items : items.slice(0, limit);
  return (
    <div className="min-w-0">
      <Tag id={id} className={className} data-testid={testid}>
        {shown}
      </Tag>
      {hidden > 0 && (
        <button
          type="button"
          aria-expanded={open}
          aria-controls={id}
          onClick={() => setOpen(!open)}
          className="arc-action arc-press mt-2 inline-flex min-h-[32px] items-center max-tablet:min-h-[44px]"
          data-testid={testid ? `${testid}-more` : undefined}
        >
          {open ? "Show less" : `Show ${hidden} more${noun ? ` ${noun}` : ""}`}
        </button>
      )}
    </div>
  );
}
