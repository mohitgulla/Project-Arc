import { describe, expect, it } from "vitest";

import { CAT } from "./personas.fixture";

import {
  OPS_CLOSED_BY_DEFAULT,
  OPS_WIDGETS,
  contextGroups,
  kindLabel,
  shortList,
  bandHasProblem,
  bandRollup,
  bandRows,
  bandStartsOpen,
  configGroups,
  contractRows,
  dayParam,
  externalInputs,
  filterLog,
  formatDuration,
  groupRepeats,
  llmBars,
  loopSplit,
  manifestGroups,
  nowPct,
  rowFacts,
  rowSummary,
  runApiQuery,
  runQuery,
  runStatusLabel,
  runStatusTone,
  pxPerHour,
  scaleTicks,
  nearestSlot,
  scrollToNow,
  sharePct,
  slotCounts,
  slotTitle,
  sourceGroups,
  timeLeft,
  timelinePct,
  windowHours,
  windowLabel,
  windowParam,
  worstStatus,
  type Llm,
  type OpsConfig,
  type Session,
  type Slot,
  type SourceRow,
  type Sources,
  type TimelineBand,
  type TimelineRow,
} from "./ops";

const START = "2026-09-30T06:00:00-04:00";
const END = "2026-09-30T22:00:00-04:00";

function slot(status: Slot["status"], at = "2026-09-30T10:00:00-04:00", run: Partial<NonNullable<Slot["run"]>> | null = null): Slot {
  return {
    job: "research",
    at,
    status,
    chain_steps: 0,
    run: run
      ? {
          run_id: "run-1",
          job: "research",
          status: "ok",
          no_change: false,
          reason: "schedule",
          scheduled_for: at,
          started_at: at,
          finished_at: at,
          duration_ms: 95_000,
          summary: "shortlist 1",
          error: null,
          chain_run_id: "chain-1",
          step_index: 0,
          attempts: 1,
          route: "/ops/runs/run-1",
          ...run,
        }
      : null,
  };
}

describe("timeline geometry", () => {
  it("maps times onto the window and clamps outside it", () => {
    expect(timelinePct(START, START, END)).toBe(0);
    expect(timelinePct(END, START, END)).toBe(100);
    expect(timelinePct("2026-09-30T14:00:00-04:00", START, END)).toBe(50);
    expect(timelinePct("2026-09-30T03:00:00-04:00", START, END)).toBe(0);
    expect(timelinePct("2026-09-30T23:30:00-04:00", START, END)).toBe(100);
    expect(timelinePct("nope", START, END)).toBe(0);
  });

});

describe("slots", () => {
  it("hover text carries job, time, status, duration and outcome", () => {
    expect(slotTitle(slot("future"))).toBe("research 10:00 ET · Upcoming");
    const s = { ...slot("done", undefined, {}), chain_steps: 4 };
    expect(slotTitle(s)).toBe("research 10:00 ET · Done · 1 min 35 s · 4 chain steps — shortlist 1");
    expect(slotTitle(slot("failed", undefined, { status: "failed", error: "HTTPError: 503" }))).toContain("— HTTPError: 503");
  });

  it("counts statuses in display order, dropping zeros", () => {
    expect(slotCounts({ future: 3, done: 2, no_change: 5 })).toEqual([
      { status: "done", n: 2 },
      { status: "no_change", n: 5 },
      { status: "future", n: 3 },
    ]);
  });

  it("splits the loop row into full vs no_change runs", () => {
    const row = { job: "research", label: "research", kind: "loop", cadence: "", slots: [slot("done"), slot("no_change"), slot("no_change"), slot("failed"), slot("future")] } as TimelineRow;
    expect(loopSplit(row)).toEqual({ full: 1, noChange: 2, failed: 1 });
    expect(loopSplit(null)).toEqual({ full: 0, noChange: 0, failed: 0 });
  });
});

describe("formatting", () => {
  it("formats durations", () => {
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(850)).toBe("850 ms");
    expect(formatDuration(42_000)).toBe("42 s");
    expect(formatDuration(180_000)).toBe("3 min");
    expect(formatDuration(3_720_000)).toBe("1 h 2 min");
  });

  it("formats TTL left", () => {
    const now = Date.parse("2026-09-30T12:00:00-04:00");
    expect(timeLeft(null, now)).toBe("no expiry");
    expect(timeLeft("2026-09-30T11:00:00-04:00", now)).toBe("expired");
    expect(timeLeft("2026-09-30T12:30:00-04:00", now)).toBe("30 min left");
  });

});

describe("day + run filters", () => {
  it("reads the day control", () => {
    expect(dayParam(null)).toBe("today");
    expect(dayParam("yesterday")).toBe("yesterday");
    expect(dayParam("2026-09-28")).toBe("2026-09-28");
    expect(dayParam("garbage")).toBe("today");
  });

  it("round-trips run filters into the API query", () => {
    const q = runQuery(new URLSearchParams("job=research,edgar&status=no_change&rday=today&chain=c1&page=3"));
    expect(q).toEqual({ job: ["research", "edgar"], status: ["no_change"], day: "today", chain: "c1", page: 3 });
    expect(runApiQuery(q)).toEqual({ page: 3, size: 50, job: "research,edgar", status: "no_change", day: "today", chain: "c1" });
    expect(runApiQuery(runQuery(new URLSearchParams("page=-2")))).toEqual({ page: 1, size: 50 });
  });

  it("tones run statuses; no_change reads as neutral", () => {
    expect(runStatusTone({ status: "ok", no_change: false })).toBe("pos");
    expect(runStatusTone({ status: "ok", no_change: true })).toBe("neutral");
    expect(runStatusLabel({ status: "ok", no_change: true })).toBe("no change");
    expect(runStatusTone({ status: "failed", no_change: false })).toBe("neg");
    expect(runStatusTone({ status: "running", no_change: false })).toBe("warn");
  });
});

describe("run detail", () => {
  it("groups the manifest in the manifest's own order, formatting units", () => {
    const g = manifestGroups({
      run_id: "run-1",
      job: "research",
      duration_ms: 95_000,
      status: "ok",
      git_sha: "0123456789abcdef0123",
      cost_usd: 0.0321,
      input_tokens: 12345,
      config_hashes: { "routines.yaml": "a1b2" },
      snapshot_ids: [],
    });
    expect(g.map((x) => x.title)).toEqual(["Identity & Trigger", "Timing & Session", "Outcome", "Code & Config", "Inputs", "LLM"]);
    const flat = Object.fromEntries(g.flatMap((x) => x.rows.map((r) => [r.label, r.value])));
    expect(flat).toMatchObject({
      Duration: "1 min 35 s",
      "Git sha": "0123456789ab",
      Cost: "$0.03",
      "Input tokens": "12,345",
      "Config hashes": "routines.yaml: a1b2",
      Snapshots: "—",
    });
    expect(manifestGroups(null)).toEqual([]);
  });

  it("flags undeclared kinds in declared-vs-actual", () => {
    const rows = contractRows(["shortlist", "regime"], ["proposal", "shortlist"], ["proposal"]);
    expect(rows).toEqual([
      { kind: "proposal", declared: false, used: true, mismatch: true },
      { kind: "regime", declared: true, used: false, mismatch: false },
      { kind: "shortlist", declared: true, used: true, mismatch: false },
    ]);
    expect(contractRows(null, ["candidate"], [])).toEqual([{ kind: "candidate", declared: false, used: true, mismatch: false }]);
  });

  it("lists external inputs", () => {
    expect(externalInputs({ external_inputs: [{ name: "chain:SPY", source: "alpaca", as_of: "t", digest: "abcdef0123456789", count: 412 }] })).toEqual([
      { name: "chain:SPY", source: "alpaca", asOf: "t", digest: "abcdef012345", count: "412" },
    ]);
  });

  it("filters the log by minimum level", () => {
    const lines = [{ level: "debug" }, { level: "info" }, { level: "warning" }, { level: "error" }];
    expect(filterLog(lines, "warning")).toEqual([{ level: "warning" }, { level: "error" }]);
    expect(filterLog(lines, "debug")).toHaveLength(4);
  });
});

describe("llm, config", () => {
  it("stacks LLM cost by persona per day", () => {
    const llm = {
      personas: ["research", "scalp"],
      series: [
        { day: "2026-09-29", calls: 2, input_tokens: 1, output_tokens: 1, cost_usd: 0.3, by_persona: { research: 0.1, scalp: 0.2 }, by_model: {} },
        { day: "2026-09-30", calls: 1, input_tokens: 1, output_tokens: 1, cost_usd: 0.1, by_persona: { research: 0.1 }, by_model: {} },
      ],
    } as unknown as Llm;
    const b = llmBars(llm, CAT);
    expect(b.data).toEqual([
      { label: "09-29", research: 0.1, scalp: 0.2 },
      { label: "09-30", research: 0.1, scalp: 0 },
    ]);
    expect(b.series.map((s) => s.label)).toEqual(["Research", "Scalp"]);
  });

  it("groups config keys with overrides first", () => {
    const k = (key: string, group: string, source: "yaml" | "override") => ({ key, group, source }) as OpsConfig["keys"][number];
    const g = configGroups([k("a", "risk", "yaml"), k("b", "risk", "override"), k("c", "scalp", "yaml")]);
    expect(g.map((x) => [x.group, x.keys.map((y) => y.key)])).toEqual([
      ["risk", ["b", "a"]],
      ["scalp", ["c"]],
    ]);
  });
});

// ---- E8.8d -------------------------------------------------------------------------

const T = (hhmm: string) => `2026-09-30T${hhmm}:00-04:00`;

function eSlot(status: Slot["status"], hhmm = "10:00", job = "x"): Slot {
  return { job, at: T(hhmm), status, chain_steps: 0, run: null } as Slot;
}

function eRow(job: string, band: string, extra: Partial<TimelineRow> = {}): TimelineRow {
  return {
    job,
    label: job,
    kind: "persona",
    cadence: "every 30m",
    slots: [],
    group: band.split(".")[0]!,
    band,
    persona: null,
    about: null,
    window: null,
    llm: false,
    writes: [],
    categories: [],
    ...extra,
  } as TimelineRow;
}

function band(key: string, label: string, group: string, groupLabel: string, jobs: string[]): TimelineBand {
  return { key, label, group, group_label: groupLabel, jobs };
}

describe("E8.8d widget order", () => {
  it("is the owner's order (snapshot)", () => {
    expect(OPS_WIDGETS).toMatchInlineSnapshot(`
      [
        "Session Timeline",
        "Sources",
        "LLM Usage",
        "Context Store",
        "Auto-Approve",
        "Config",
        "Health",
        "Alerts",
        "Halts",
        "Runs",
      ]
    `);
    expect(OPS_CLOSED_BY_DEFAULT).toEqual(["Alerts", "Halts", "Runs"]);
    expect(OPS_WIDGETS).not.toContain("Order Budget");
  });
});

describe("D89 context store groups", () => {
  const kind = (k: string, group: string, active: number, latest: string | null) => ({
    kind: k,
    active,
    ttl: null,
    next_expiry: null,
    remaining_fraction: null,
    latest_id: latest ? `ctx-${k}` : null,
    latest_subject: latest ? "s" : null,
    latest_at: latest,
    latest_by: null,
    expired_24h: 0,
    group,
    writers: [],
    readers: [],
  });
  it("joins kinds to the API's group order, active kinds first, unknown groups under Other", () => {
    const g = contextGroups({
      groups: [
        { key: "sources", label: "Sources", active: 3, kinds: 2 },
        { key: "universe", label: "Universe", active: 1, kinds: 1 },
      ],
      kinds: [
        kind("vol_term", "sources", 0, "2026-10-09T08:20:00Z"),
        kind("raw_doc_ref", "sources", 3, "2026-10-09T20:00:00Z"),
        kind("universe_tier", "universe", 1, "2026-10-09T10:00:00Z"),
        kind("mystery", "nowhere", 2, null),
      ],
    });
    expect(g.map((x) => [x.key, x.active, x.kinds.map((k) => k.kind)])).toEqual([
      ["sources", 3, ["raw_doc_ref", "vol_term"]],
      ["universe", 1, ["universe_tier"]],
      ["other", 2, ["mystery"]],
    ]);
    expect(g[0]?.latestAt).toBe("2026-10-09T20:00:00Z");
  });
  it("labels kinds and shortens long writer lists", () => {
    expect(kindLabel("raw_doc_ref")).toBe("Raw doc ref");
    expect(shortList(["RSS news", "EDGAR", "Ticker news", "Earnings"], 2)).toBe("RSS news, EDGAR +2");
    expect(shortList([], 2)).toBe("—");
  });
});

describe("E8.8d timeline bands", () => {
  // The API assigns bands from routines.yaml (category for sources, group: for the rest,
  // fallback other); the UI joins rows to bands in the API's order.
  const session = {
    loop: eRow("research", "trading_loop", { persona: "research", label: "Research → Quant → Risk → Propose → Execute" }),
    rows: [
      eRow("rss", "sources.market_news", { kind: "source", categories: ["market_news", "company_data"] }),
      eRow("edgar", "sources.company_data", { kind: "source" }),
      eRow("scalp", "scalp", { persona: "scalp" }),
      eRow("positions.evaluate", "position_management", { persona: "quant", label: "Position marks" }),
      eRow("mystery", "other"),
      eRow("orphan", "nowhere"), // no band from the API: falls under Other
    ],
    bands: [
      band("sources.market_news", "Market news", "sources", "Sources", ["rss"]),
      band("sources.company_data", "Company data", "sources", "Sources", ["edgar"]),
      band("scalp", "Scalp", "scalp", "Scalp", ["scalp"]),
      band("trading_loop", "Trading loop", "trading_loop", "Trading loop", ["research"]),
      band("position_management", "Position management", "position_management", "Position management", ["positions.evaluate"]),
      band("other", "Other", "other", "Other", ["mystery"]),
    ],
  } as unknown as Session;

  it("keeps the API's band order and puts the loop row in its band", () => {
    const v = bandRows(session);
    expect(v.map((b) => b.band.key)).toEqual([
      "sources.market_news",
      "sources.company_data",
      "scalp",
      "trading_loop",
      "position_management",
      "other",
    ]);
    expect(v.find((b) => b.band.key === "trading_loop")!.rows.map((r) => r.job)).toEqual(["research"]);
  });

  it("marks category sub-bands under the Sources group, once", () => {
    const v = bandRows(session);
    expect(v.filter((b) => b.sub).map((b) => [b.band.key, b.firstOfGroup])).toEqual([
      ["sources.market_news", true],
      ["sources.company_data", false],
    ]);
    expect(v.find((b) => b.band.key === "scalp")!.sub).toBe(false);
  });

  it("falls back to Other for rows without a band", () => {
    const other = bandRows(session).at(-1)!;
    expect(other.band.key).toBe("other");
    expect(other.rows.map((r) => r.job)).toEqual(["mystery", "orphan"]);
  });

  it("creates an Other band when the API sent none", () => {
    const v = bandRows({ ...session, bands: [] });
    expect(v).toHaveLength(1);
    expect(v[0]!.band.label).toBe("Other");
    expect(v[0]!.rows).toHaveLength(7);
  });

  it("rolls a band up over settled slots and opens it on a problem", () => {
    const rows = [
      eRow("a", "scalp", { slots: [eSlot("done", "06:00"), eSlot("no_change", "07:00"), eSlot("missed", "08:00"), eSlot("future", "16:00")] }),
      eRow("b", "scalp", { slots: [eSlot("done", "06:30")] }),
    ];
    expect(bandRollup(rows)).toBe("3/4 ok · 1 missed");
    expect(bandHasProblem(rows)).toBe(true);
    expect(bandHasProblem([rows[1]!])).toBe(false);
    expect(bandRollup([eRow("c", "scalp", { slots: [eSlot("future", "21:00")] })])).toBe("next 21:00");
  });

  it("summarises a job row and lists its ⓘ facts", () => {
    const r = eRow("rss", "sources.market_news", {
      slots: [eSlot("done", "09:00"), eSlot("failed", "09:15"), eSlot("missed", "09:30"), eSlot("future", "10:30"), eSlot("future", "10:45")],
      window: "09:00-16:00",
      llm: false,
      writes: [],
    });
    expect(rowSummary(r)).toBe("1/5 done · 1 failed · 1 missed · next 10:30");
    expect(rowFacts(r)).toEqual(["every 30m", "window 09:00-16:00", "LLM no", "writes nothing"]);
    expect(rowFacts(eRow("scalp", "scalp", { llm: true, writes: ["story"] }))).toEqual(["every 30m", "LLM yes", "writes story"]);
  });
});

describe("E8.8d sources by category", () => {
  const src = (key: string, category: string, status: SourceRow["status"]): SourceRow =>
    ({ key, label: key, category, status, share_in_category: 0.5 }) as SourceRow;

  it("worst status wins the rollup", () => {
    expect(worstStatus(["ok", "idle", "ok"])).toBe("idle");
    expect(worstStatus(["ok", "late", "backoff", "pending"])).toBe("late");
    expect(worstStatus(["failed", "late"])).toBe("failed");
    expect(worstStatus([])).toBe("ok");
  });

  it("groups rows under the API's categories, in its order (rename-safe)", () => {
    const s = {
      categories: [
        { key: "youtube_macro", label: "YouTube macro", weight: 1, share: null, max_age: "24h", newest_doc_at: null, status: "pending", sources: 1 },
        { key: "market_news", label: "Market news", weight: 1, share: 0.25, max_age: "6h", newest_doc_at: null, status: "ok", sources: 1 },
        { key: "empty", label: "Empty", weight: 1, share: 0.25, max_age: "6h", newest_doc_at: null, status: "ok", sources: 0 },
      ],
      sources: [src("wsj", "market_news", "ok"), src("youtube.a", "youtube_macro", "pending"), src("x", "brand_new", "late")],
    } as unknown as Sources;
    const g = sourceGroups(s);
    expect(g.map((x) => x.category.key)).toEqual(["youtube_macro", "market_news", "brand_new"]);
    expect(g[2]!.category.status).toBe("late");
  });

  it("formats shares", () => {
    expect(sharePct(0.2)).toBe("20 %");
    expect(sharePct(0.001)).toBe("<1 %");
    expect(sharePct(null)).toBe("—");
  });
});

describe("E8.8d repeat grouping", () => {
  it("collapses repeats like the overview (`missed_window ×12`)", () => {
    const items = [
      ...Array.from({ length: 12 }, (_, i) => ({ id: `m${i}`, kind: "missed_window" })),
      { id: "s", kind: "stuck" },
    ];
    const g = groupRepeats(items, (a) => a.kind);
    expect(g.map((x) => x.text)).toEqual(["missed_window ×12", "stuck"]);
    expect(g[0]!.items).toHaveLength(12);
  });
});

describe("D93 interactive timeline", () => {
  const RS = "2026-09-30T05:00:00-04:00";
  const RE = "2026-09-30T18:00:00-04:00";

  it("defaults to Rolling; ?tl=day or an old ?day= link opens the Day view", () => {
    expect(windowParam(null, null)).toBe("rolling");
    expect(windowParam(null, "today")).toBe("rolling");
    expect(windowParam(null, "yesterday")).toBe("day");
    expect(windowParam(null, "2026-09-28")).toBe("day");
    expect(windowParam("day", null)).toBe("day");
    expect(windowParam("rolling", "yesterday")).toBe("rolling");
    expect(windowParam("week", null)).toBe("rolling");
  });

  it("ticks every hour on the ET wall clock and marks midnight with the weekday", () => {
    const t = scaleTicks(RS, RE);
    expect(t).toHaveLength(14);
    expect(t[0]).toEqual({ label: "05:00", pct: 0, day: null });
    expect(t.at(-1)!.label).toBe("18:00");
    const over = scaleTicks("2026-09-29T18:00:00-04:00", "2026-09-30T07:00:00-04:00");
    const mid = over.find((x) => x.label === "00:00")!;
    expect(mid.day).toBe("Wed");
    expect(over.filter((x) => x.day)).toHaveLength(1);
  });

  it("spans 24 ticks + 1 on a full day and 23 on the spring-forward day", () => {
    expect(scaleTicks("2026-09-30T00:00:00-04:00", "2026-10-01T00:00:00-04:00")).toHaveLength(25);
    expect(windowHours("2026-03-08T00:00:00-05:00", "2026-03-09T00:00:00-04:00")).toBe(23);
  });

  it("places the now-bar only inside the window", () => {
    expect(nowPct(Date.parse("2026-09-30T13:42:00-04:00"), RS, RE)).toBeCloseTo((8.7 / 13) * 100, 5);
    expect(nowPct(Date.parse("2026-09-30T19:00:00-04:00"), RS, RE)).toBeNull();
    expect(nowPct(Date.parse("2026-09-30T04:59:00-04:00"), RS, RE)).toBeNull();
  });

  it("scrolls the now-bar to two thirds of the viewport, clamped", () => {
    expect(scrollToNow(50, 1000, 300)).toBe(300);
    expect(scrollToNow(5, 1000, 300)).toBe(0);
    expect(scrollToNow(100, 1000, 300)).toBe(700);
    expect(scrollToNow(null, 1000, 300)).toBe(0);
    expect(scrollToNow(50, 300, 400)).toBe(0); // fits: no scroll
  });

  it("labels the window, across midnight too", () => {
    expect(windowLabel(RS, RE)).toBe("Wed 05:00 – 18:00 ET");
    expect(windowLabel("2026-09-30T00:00:00-04:00", "2026-10-01T00:00:00-04:00")).toBe("Wed 00:00 – 24:00 ET");
    expect(windowLabel("2026-09-29T18:00:00-04:00", "2026-09-30T07:00:00-04:00")).toBe("Tue 18:00 – Wed 07:00 ET");
  });

  it("resolves a tap to the nearest slot, so overlapping targets never open a neighbour", () => {
    const at = (hm: string) => ({ at: `2026-09-30T${hm}:00-04:00` });
    const slots = [at("09:40"), at("09:45"), at("09:50")];
    const pctOf = (hm: string) => timelinePct(`2026-09-30T${hm}-04:00`, RS, RE);
    expect(nearestSlot(slots, pctOf("09:44:00"), RS, RE)).toBe(slots[1]);
    expect(nearestSlot(slots, pctOf("09:42:00"), RS, RE)).toBe(slots[0]);
    expect(nearestSlot(slots, pctOf("12:00:00"), RS, RE)).toBe(slots[2]);
    expect(nearestSlot([], 50, RS, RE)).toBeNull();
    expect(pxPerHour(true)).toBe(120);
    expect(pxPerHour(false)).toBe(96);
  });

  it("opens every group on desktop; on a phone only the ones with a problem", () => {
    const bad = [eRow("a", "scalp", { slots: [eSlot("missed", "06:00")] })];
    const ok = [eRow("b", "scalp", { slots: [eSlot("done", "06:00")] })];
    expect(bandStartsOpen(ok, false)).toBe(true);
    expect(bandStartsOpen(ok, true)).toBe(false);
    expect(bandStartsOpen(bad, true)).toBe(true);
  });
});
