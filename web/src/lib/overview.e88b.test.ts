// E8.8b (D48): Overview status row model, Greeks used/cap, and the "Orders today" grep.
import { describe, expect, it } from "vitest";

import { shortAge, statusRow, usedOfCap, type Overview } from "./overview";

const NOW = Date.parse("2026-09-28T19:40:00Z"); // Mon 15:40 ET
const min = (m: number) => new Date(NOW - m * 60_000).toISOString();

function status(over: Partial<Overview["status"]> = {}): Pick<Overview, "status"> {
  return {
    status: {
      halted: false,
      halt: null,
      active_halts: 0,
      tick_at: min(3),
      tick_status: "ok",
      health_at: min(7),
      health_status: "ok",
      alerts: [],
      order_budget: { used: 8, limit: 200, tier: "normal", as_of: min(2) },
      ...over,
    },
  };
}

const CTX = { now: NOW, tickS: 300, healthS: 1800, env: "paper", accountProfile: "cash_debit" };

describe("E8.8b status row", () => {
  it("has the fixed slot order and plain `label value` text", () => {
    const row = statusRow(status(), CTX);
    expect(row.map((s) => s.key)).toEqual(["trading", "tick", "health", "alerts", "orders", "env"]);
    expect(row.map((s) => `${s.label} ${s.value}`.trim())).toEqual([
      "Trading enabled",
      "Tick 3m",
      "Health ok 7m",
      "Alerts 0",
      "Orders 8/200",
      "paper · cash_debit",
    ]);
    expect(row.every((s) => s.tone === "ok" || s.key === "env")).toBe(true);
  });

  it("a halt replaces slot 1 with HALTED + reason + age", () => {
    const row = statusRow(
      status({
        halted: true,
        active_halts: 2,
        halt: { id: "h", kind: "manual", reason: "reconcile mismatch", actor: "arc:reconcile", at: min(70), active: true },
      } as Partial<Overview["status"]>),
      CTX,
    );
    expect(row).toHaveLength(6);
    expect(row[0]).toMatchObject({ key: "trading", label: "HALTED", value: "1h", tone: "neg", detail: "reconcile mismatch" });
    expect(row[0]?.title).toContain("arc:reconcile");
    expect(row[0]?.title).toContain("2 active");
    expect(row[1]?.key).toBe("tick");
  });

  it("stale or non-ok heartbeats turn --warn (no pill), with the threshold in the title", () => {
    const row = statusRow(status({ tick_at: min(16), health_at: min(95) }), CTX);
    expect(row[1]).toMatchObject({ value: "16m", tone: "warn" });
    expect(row[1]?.title).toContain("stale (after 15m)");
    expect(row[2]).toMatchObject({ value: "ok 1h", tone: "warn" });
    const bad = statusRow(status({ tick_status: "partial" }), CTX);
    expect(bad[1]).toMatchObject({ value: "partial 3m", tone: "warn" });
    const none = statusRow(status({ tick_at: null }), CTX);
    expect(none[1]).toMatchObject({ value: "no data", tone: "none" });
  });

  it("alerts count, and the order tier only when it is not normal", () => {
    const alert = { kind: "missed_window", key: "k", message: "m", opened_at: min(5) };
    const row = statusRow(status({ alerts: [alert, { ...alert, key: "k2" }] }), CTX);
    expect(row[3]).toMatchObject({ value: "2", tone: "warn" });
    const tier = statusRow(status({ order_budget: { used: 182, limit: 200, tier: "throttled", as_of: null } }), CTX);
    expect(tier[4]).toMatchObject({ label: "Orders", value: "182/200 · throttled", tone: "warn" });
    const missing = statusRow(status({ order_budget: null }), CTX);
    expect(missing[4]).toMatchObject({ value: "—", tone: "none" });
  });

  it("short ages", () => {
    expect(shortAge(min(0), NOW)).toBe("now");
    expect(shortAge(min(3), NOW)).toBe("3m");
    expect(shortAge(min(60 * 26), NOW)).toBe("1d");
    expect(shortAge(null, NOW)).toBe("—");
  });

  it("Greeks rows read used / cap", () => {
    expect(usedOfCap("2.1", "300")).toBe("2.1 / 300");
    expect(usedOfCap("2.1", null)).toBe("2.1");
  });
});

// The status row says "Orders", never "Orders today" (owner, D48).
const SOURCES = import.meta.glob(["../**/*.ts", "../**/*.tsx", "!../**/*.test.ts", "!../lib/api.gen.ts"], {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

describe("E8.8b wording", () => {
  it('no "Orders today" string left in web/src', () => {
    expect(Object.keys(SOURCES).length).toBeGreaterThan(20);
    const hits = Object.entries(SOURCES)
      .filter(([file]) => !/\.test\.tsx?$/.test(file))
      .filter(([, src]) => /Orders today/i.test(src))
      .map(([f]) => f);
    expect(hits).toEqual([]);
  });
});
