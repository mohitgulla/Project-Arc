import { describe, expect, it } from "vitest";

import {
  budgetMarks,
  configGroups,
  contractRows,
  dayParam,
  externalInputs,
  filterLog,
  formatDuration,
  hourTicks,
  llmBars,
  loopSplit,
  manifestGroups,
  personaLabel,
  runApiQuery,
  runQuery,
  runStatusLabel,
  runStatusTone,
  slotCounts,
  slotTitle,
  timeLeft,
  timelinePct,
  type Llm,
  type OpsConfig,
  type Slot,
  type TimelineRow,
} from "./ops";

const START = "2026-09-30T06:00:00-04:00";
const END = "2026-09-30T22:00:00-04:00";

function slot(status: Slot["status"], at = "2026-09-30T10:00:00-04:00", run: Partial<NonNullable<Slot["run"]>> | null = null): Slot {
  return {
    job: "director",
    at,
    status,
    chain_steps: 0,
    run: run
      ? {
          run_id: "run-1",
          job: "director",
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
  it("maps times onto 06:00-22:00 and clamps outside it", () => {
    expect(timelinePct(START, START, END)).toBe(0);
    expect(timelinePct(END, START, END)).toBe(100);
    expect(timelinePct("2026-09-30T14:00:00-04:00", START, END)).toBe(50);
    expect(timelinePct("2026-09-30T03:00:00-04:00", START, END)).toBe(0);
    expect(timelinePct("2026-09-30T23:30:00-04:00", START, END)).toBe(100);
    expect(timelinePct("nope", START, END)).toBe(0);
  });

  it("labels an hour tick every two hours", () => {
    const t = hourTicks(START, END);
    expect(t.map((x) => x.label)).toEqual(["06:00", "08:00", "10:00", "12:00", "14:00", "16:00", "18:00", "20:00", "22:00"]);
    expect(t[4]?.pct).toBe(50);
  });
});

describe("slots", () => {
  it("hover text carries job, time, status, duration and outcome", () => {
    expect(slotTitle(slot("future"))).toBe("director 10:00 ET · Scheduled");
    const s = { ...slot("done", undefined, {}), chain_steps: 4 };
    expect(slotTitle(s)).toBe("director 10:00 ET · Done · 1 min 35 s · 4 chain steps — shortlist 1");
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
    const row = { job: "director", label: "director", kind: "loop", cadence: "", slots: [slot("done"), slot("no_change"), slot("no_change"), slot("failed"), slot("future")] } as TimelineRow;
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

  it("labels personas", () => {
    expect(personaLabel("director")).toBe("Director");
    expect(personaLabel("scout.digest")).toBe("Scout (digest)");
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
    const q = runQuery(new URLSearchParams("job=director,edgar&status=no_change&rday=today&chain=c1&page=3"));
    expect(q).toEqual({ job: ["director", "edgar"], status: ["no_change"], day: "today", chain: "c1", page: 3 });
    expect(runApiQuery(q)).toEqual({ page: 3, size: 50, job: "director,edgar", status: "no_change", day: "today", chain: "c1" });
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
      job: "director",
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

describe("budget, llm, config", () => {
  it("marks the D32 tiers on the budget bar", () => {
    expect(budgetMarks({ limit: 200, restrict_at: 100, open_limit: 175 })).toEqual([
      { label: "restrict 100", at: 0.5 },
      { label: "opens stop 175", at: 0.875 },
    ]);
    expect(budgetMarks({ limit: 0, restrict_at: 0, open_limit: 0 })).toEqual([]);
  });

  it("stacks LLM cost by persona per day", () => {
    const llm = {
      personas: ["director", "scout"],
      series: [
        { day: "2026-09-29", calls: 2, input_tokens: 1, output_tokens: 1, cost_usd: 0.3, by_persona: { director: 0.1, scout: 0.2 }, by_model: {} },
        { day: "2026-09-30", calls: 1, input_tokens: 1, output_tokens: 1, cost_usd: 0.1, by_persona: { director: 0.1 }, by_model: {} },
      ],
    } as unknown as Llm;
    const b = llmBars(llm);
    expect(b.data).toEqual([
      { label: "09-29", director: 0.1, scout: 0.2 },
      { label: "09-30", director: 0.1, scout: 0 },
    ]);
    expect(b.series.map((s) => s.label)).toEqual(["Director", "Scout"]);
  });

  it("groups config keys with overrides first", () => {
    const k = (key: string, group: string, source: "yaml" | "override") => ({ key, group, source }) as OpsConfig["keys"][number];
    const g = configGroups([k("a", "risk", "yaml"), k("b", "risk", "override"), k("c", "scout", "yaml")]);
    expect(g.map((x) => [x.group, x.keys.map((y) => y.key)])).toEqual([
      ["risk", ["b", "a"]],
      ["scout", ["c"]],
    ]);
  });
});
