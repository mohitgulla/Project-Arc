/**
 * E13.14 (D56): persona and category labels, from `/api/meta` only.
 *
 * The server owns every name (routines.yaml + arc/slack/personas.py) and maps stored
 * pre-rename values through arc.journal.legacy, so the SPA keeps no persona table: it
 * looks keys up in the catalogue and, for a key the catalogue lacks (a step such as
 * `scalp.digest`, or meta not loaded yet), shows the capitalised key.
 */
import { useMemo } from "react";

import type { Schemas } from "./api";
import { useMeta } from "./useApi";

export type PersonaMeta = Schemas["PersonaMeta"];
export type CategoryMeta = Schemas["CategoryMeta"];

export interface Catalogue {
  personas: PersonaMeta[];
  categories: CategoryMeta[];
}

export const EMPTY_CATALOGUE: Catalogue = { personas: [], categories: [] };

function cap(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1).replace(/_/g, " ");
}

/** The catalogue entry for a persona key or a dotted step (`risk.exit` -> risk). */
export function personaMeta(key: string, cat: Catalogue): PersonaMeta | undefined {
  const head = key.split(".")[0] ?? key;
  return cat.personas.find((p) => p.key === key) ?? cat.personas.find((p) => p.key === head);
}

/** Display name without emoji: `Research`, `Scalp (digest)`, `Risk (exit)`. */
export function personaName(key: string, cat: Catalogue): string {
  const [head = key, tail] = key.split(".");
  const meta = personaMeta(head, cat);
  const name = meta?.label ?? cap(head);
  return tail ? `${name} (${tail.replace(/_/g, " ")})` : name;
}

/** Chip text: `🧠 Research`; a persona without an emoji (monitor) is the plain name. */
export function personaLabel(key: string, cat: Catalogue): string {
  const meta = personaMeta(key, cat);
  const name = personaName(key, cat);
  return meta?.emoji ? `${meta.emoji} ${name}` : name;
}

/** Category label (`Market news`, `Reference data`); unknown keys are capitalised. */
export function categoryLabel(key: string, cat: Catalogue): string {
  return cat.categories.find((c) => c.key === key)?.label ?? cap(key);
}

/** The catalogue from `/api/meta` (empty until it loads). */
export function useCatalogue(): Catalogue {
  const meta = useMeta();
  const data = meta.data;
  return useMemo(
    () => (data ? { personas: data.personas ?? [], categories: data.categories ?? [] } : EMPTY_CATALOGUE),
    [data],
  );
}

/** `(key) => "🧠 Research"` bound to the loaded catalogue. */
export function usePersonaLabel(): (key: string) => string {
  const cat = useCatalogue();
  return useMemo(() => (key: string) => personaLabel(key, cat), [cat]);
}
