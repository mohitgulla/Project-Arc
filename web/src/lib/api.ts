/**
 * Typed client for the tower API. Types come from `/api/openapi.json` via
 * openapi-typescript (`make web-api` -> web/openapi.json -> `npm run gen:api` ->
 * api.gen.ts); a Python test fails when the committed spec drifts from the app.
 *
 * GET only: the tower is read-only (D35). Queries poll through TanStack Query
 * (see useApi.ts) at the Settings refresh interval, paused while the tab is hidden.
 */
import type { components, paths } from "./api.gen";

export type Schemas = components["schemas"];
export type Health = Schemas["HealthResponse"];
export type Meta = Schemas["MetaResponse"];
export type Cadence = Schemas["Cadence"];
export type Snapshot = Schemas["TowerSnapshot"];
export type ApiErrorBody = Schemas["ErrorResponse"];

/** Every GET path the API declares. */
export type ApiPath = keyof paths;

type JsonOf<P extends ApiPath> = paths[P] extends {
  get: { responses: { 200: { content: { "application/json": infer T } } } };
}
  ? T
  : never;

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail: string;

  constructor(status: number, code: string, detail: string) {
    super(`${status} ${code}: ${detail}`);
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

function isErrorBody(x: unknown): x is ApiErrorBody {
  return typeof x === "object" && x !== null && "error" in x && "detail" in x;
}

export async function apiGet<P extends ApiPath>(
  path: P,
  init: { query?: Record<string, string | number | undefined>; signal?: AbortSignal } = {},
): Promise<JsonOf<P>> {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(init.query ?? {})) if (v !== undefined) qs.set(k, String(v));
  const url = qs.size ? `${path}?${qs.toString()}` : path;
  const res = await fetch(url, {
    method: "GET",
    headers: { Accept: "application/json" },
    credentials: "same-origin",
    signal: init.signal,
  });
  const body: unknown = await res.json().catch(() => null);
  if (!res.ok) {
    if (isErrorBody(body)) throw new ApiError(res.status, body.error, body.detail);
    throw new ApiError(res.status, `http_${res.status}`, res.statusText);
  }
  return body as JsonOf<P>;
}

/** Decimal fields arrive as strings (exact); convert for display only. */
export function num(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : null;
}
