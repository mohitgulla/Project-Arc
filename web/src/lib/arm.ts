/**
 * E15.7 (D69, D92): the global arm switcher's pure helpers.
 *
 * The Tower reads one account at a time. Without `?arm=` (or with `arm=control`) it is the
 * production account, shown as **Arc Investor** (D92, never "Control" in UI text). With
 * `?arm=<experiment id>:<arm>` (e.g. `XP-12:t1`) every API call reads that experiment arm's
 * virtual account (E15.6). The choice lives in the URL only, so a link is shareable and every
 * fresh load without `?arm` is Arc Investor (owner: no localStorage persistence).
 */
import type { Schemas } from "./api";
import { ACCOUNT_NAME, armColor } from "./experiments";

export type ArmMeta = Schemas["ArmMeta"];

/** The query/URL parameter (E15.6 API contract). */
export const ARM_PARAM = "arm";
/** The API's internal name for the production account (UI shows {@link ACCOUNT_NAME}). */
export const CONTROL = "control";

/** `?arm=` value from a location search string; `null` = Arc Investor. */
export function parseArm(search: string): string | null {
  const v = new URLSearchParams(search).get(ARM_PARAM)?.trim() ?? "";
  return v === "" || v === CONTROL ? null : v;
}

/** *search* with `arm` set to *arm* (or removed for Arc Investor); keeps every other param. */
export function searchWithArm(search: string, arm: string | null): string {
  const p = new URLSearchParams(search);
  if (arm) p.set(ARM_PARAM, arm);
  else p.delete(ARM_PARAM);
  const s = p.toString();
  return s ? `?${s}` : "";
}

/** Adds `arm` to an API query object (nothing for Arc Investor, so control URLs are unchanged). */
export function withArm<Q extends Record<string, string | number | undefined>>(
  query: Q | undefined,
  arm: string | null,
): Q & { arm?: string } {
  return arm ? { ...(query ?? ({} as Q)), arm } : { ...(query ?? ({} as Q)) };
}

/** `XP-12:t1` -> `XP-12 · T1` when the arm list is not loaded yet (the API's `label` wins). */
export function fallbackArmLabel(arm: string): string {
  const [xp, name] = arm.includes(":") ? [arm.slice(0, arm.lastIndexOf(":")), arm.slice(arm.lastIndexOf(":") + 1)] : ["", arm];
  const short = name === "treatment" ? "T1" : /^t\d+$/i.test(name) ? name.toUpperCase() : name;
  return xp ? `${xp} · ${short}` : short;
}

/** Display name of the account under view: Arc Investor, or the arm's treatment name. */
export function viewLabel(arm: string | null, arms: readonly ArmMeta[] | undefined): string {
  if (!arm) return ACCOUNT_NAME;
  return arms?.find((a) => a.id === arm)?.label ?? fallbackArmLabel(arm);
}

/** The arms a viewer can switch to (running, store present), in the API's order. */
export function viewableArms(arms: readonly ArmMeta[] | undefined): ArmMeta[] {
  return (arms ?? []).filter((a) => a.viewable);
}

/**
 * Whether *arm* can be viewed: `true` for Arc Investor, `undefined` while the arm list is
 * loading, `false` for an unknown / stopped / store-less arm (the API answers it with 404).
 */
export function armIsViewable(arm: string | null, arms: readonly ArmMeta[] | undefined): boolean | undefined {
  if (!arm) return true;
  if (!arms) return undefined;
  return viewableArms(arms).some((a) => a.id === arm);
}

/** Series colour of an arm, matching the Experiments page (Arc Investor in the accent). */
export function armDot(arm: ArmMeta | null, arms: readonly ArmMeta[]): string {
  if (!arm) return armColor(CONTROL, [CONTROL]);
  const order = [CONTROL, ...arms.filter((a) => a.experiment_id === arm.experiment_id).map((a) => a.arm)];
  return armColor(arm.arm, order);
}

/** Banner text while an arm is under view (D92: "Arc Investor", no "Control"). */
export function bannerText(label: string): string {
  return `Viewing ${label} — virtual account, shared inputs from ${ACCOUNT_NAME}`;
}
