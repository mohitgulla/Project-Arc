// E8.8a (D48): Title Case widget titles and the header freshness badge (TOWER_DESIGN §10).
import { describe, expect, it } from "vitest";

import { freshnessView, isTitleCase, olderSource, titleCase } from "./format";

describe("§10 titleCase", () => {
  it("capitalises ordinary words", () => {
    expect(titleCase("Equity curve")).toBe("Equity Curve");
    expect(titleCase("recent activity")).toBe("Recent Activity");
    expect(titleCase("Today's proposals")).toBe("Today's Proposals");
  });

  it("keeps small words lower case inside, capitalised at the edges", () => {
    expect(titleCase("Greeks vs caps")).toBe("Greeks vs Caps");
    expect(titleCase("Modelled vs realised")).toBe("Modelled vs Realised");
    expect(titleCase("Spec and hashes")).toBe("Spec and Hashes");
    expect(titleCase("Equity from t0")).toBe("Equity from t0");
    expect(titleCase("Context read / written")).toBe("Context Read / Written");
    expect(titleCase("the end of a day")).toBe("The End of a Day");
    expect(titleCase("what to")).toBe("What To");
    expect(titleCase("P&L per day")).toBe("P&L per Day");
  });

  it("leaves acronyms and mixed-case tokens as written", () => {
    expect(titleCase("P&L today")).toBe("P&L Today");
    expect(titleCase("LLM usage")).toBe("LLM Usage");
    expect(titleCase("Net EV and PoP")).toBe("Net EV and PoP");
    expect(titleCase("Max loss by ET hour")).toBe("Max Loss by ET Hour");
  });

  it("handles & and / separators", () => {
    expect(titleCase("Win / loss")).toBe("Win / Loss");
    expect(titleCase("Gate & funnel")).toBe("Gate & Funnel");
    expect(titleCase("Outcome & review")).toBe("Outcome & Review");
    expect(titleCase("Identity & trigger")).toBe("Identity & Trigger");
  });

  it("capitalises each part of a hyphenated word", () => {
    expect(titleCase("Auto-approve")).toBe("Auto-Approve");
  });

  it("is idempotent and detects sentence case", () => {
    for (const s of ["Greeks vs Caps", "Win / Loss", "P&L Today", "Auto-Approve", "Equity from t0"]) {
      expect(titleCase(s)).toBe(s);
      expect(isTitleCase(s)).toBe(true);
    }
    expect(isTitleCase("Greeks vs caps")).toBe(false);
    expect(isTitleCase("Session timeline")).toBe(false);
  });
});

describe("§10 freshness badge", () => {
  const NOW = Date.parse("2026-10-02T20:02:00Z"); // Fri 10-02 16:02 ET
  const AT = "2026-10-02T19:59:00Z"; // Fri 10-02 15:59 ET, 3 min earlier

  it("fresh: compact age, full line with source and stale threshold", () => {
    expect(freshnessView({ at: AT, cadenceS: 300, label: "monitor mark" }, NOW)).toEqual({
      state: "fresh",
      compact: "3m ago",
      full: "as of Fri 10-02 15:59 ET · monitor mark · stale after 15m",
    });
  });

  it("stale past 3x the cadence", () => {
    const v = freshnessView({ at: "2026-10-01T20:00:00Z", cadenceS: 300, label: "monitor mark" }, NOW);
    expect(v.state).toBe("stale");
    expect(v.compact).toBe("stale · 1d ago");
  });

  it("never stale without a cadence, unless forced", () => {
    expect(freshnessView({ at: "2026-09-01T20:00:00Z" }, NOW).state).toBe("fresh");
    expect(freshnessView({ at: AT }, NOW, { stale: true }).state).toBe("stale");
    expect(freshnessView({ at: AT }, NOW).full).toBe("as of Fri 10-02 15:59 ET");
  });

  it("no data", () => {
    expect(freshnessView({ at: null, label: "reconcile" }, NOW)).toEqual({
      state: "none",
      compact: "no data",
      full: "reconcile: no data yet",
    });
    expect(freshnessView({ at: "not a time" }, NOW).state).toBe("none");
  });

  it("a mixed widget badges the older source", () => {
    const realized = { at: "2026-10-01T20:30:00Z", label: "reconcile" };
    const unrealized = { at: AT, label: "monitor mark" };
    expect(olderSource([unrealized, realized])).toBe(realized);
    expect(olderSource([{ at: null }, unrealized])).toBe(unrealized);
    expect(olderSource([{ at: null }])).toBeNull();
  });
});
