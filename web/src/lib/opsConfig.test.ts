// E8.8e: list-diff formatting, actor name mapping, section grouping and the run-detail
// context counts (/ops/config, /ops/runs/:runId).
import { describe, expect, it } from "vitest";

import type { OpsConfig, StepView } from "./ops";
import {
  actorName,
  allowedText,
  changeHeadline,
  changeMatches,
  configSections,
  isHashLike,
  keyMatches,
  keySegments,
  kindCounts,
  kindCountsText,
  listDiff,
  listDiffText,
  listNoun,
  riskLabel,
  shortHash,
  sourceLabel,
  tailLines,
  type ConfigChange,
  type ConfigKey,
} from "./opsConfig";

function key(k: string, group: string, extra: Partial<ConfigKey> = {}): ConfigKey {
  return {
    key: k,
    group,
    value: 1,
    value_text: "1",
    default: 1,
    default_text: "1",
    source: "yaml",
    bounds: "-",
    risk: "up",
    description: `${k} description`,
    value_type: "int",
    is_list: false,
    choices: [],
    ...extra,
  } as ConfigKey;
}

function change(extra: Partial<ConfigChange> = {}): ConfigChange {
  return {
    id: 1,
    key: "max_open_positions",
    old: 5,
    new: 4,
    old_text: "5",
    new_text: "4",
    is_default: false,
    actor: "U0C5KUMH28G",
    reason: null,
    at: "2026-09-30T10:00:00-04:00",
    source: "slack",
    status: "applied",
    supersedes_id: null,
    direction: "safer",
    halted: false,
    group: "risk",
    is_list: false,
    ...extra,
  } as ConfigChange;
}

describe("actor names (tower.actor_names)", () => {
  const names = { U0C5KUMH28G: "Mohit" };
  it("maps a known Slack id to its display name", () => {
    expect(actorName("U0C5KUMH28G", names)).toBe("Mohit");
  });
  it("shows an unknown id as is, and a dash for none", () => {
    expect(actorName("U0OTHER", names)).toBe("U0OTHER");
    expect(actorName("cli", {})).toBe("cli");
    expect(actorName(null, names)).toBe("—");
    expect(actorName("U0C5KUMH28G", undefined)).toBe("U0C5KUMH28G");
  });
  it("labels the source", () => {
    expect([sourceLabel("slack"), sourceLabel("cli"), sourceLabel("api")]).toEqual(["Slack", "CLI", "Api"]);
  });
  it("filters changes by display name as well as the id", () => {
    expect(changeMatches(change(), "mohit", names)).toBe(true);
    expect(changeMatches(change(), "u0c5k", names)).toBe(true);
    expect(changeMatches(change(), "nobody", names)).toBe(false);
  });
});

describe("list diffs", () => {
  const old = ["SPY", "QQQ", "DIA", "XLF"];
  const neu = ["SPY", "QQQ", "SMH", "SCHD"];
  it("adds and removes in each list's own order", () => {
    expect(listDiff(old, neu)).toEqual({ added: ["SMH", "SCHD"], removed: ["DIA", "XLF"] });
  });
  it("renders +A −B with a real minus sign", () => {
    expect(listDiffText(old, neu)).toBe("+SMH +SCHD \u2212DIA \u2212XLF");
    expect(listDiffText(old, old)).toBe("no member change");
    expect(listDiffText(null, ["A"])).toBe("+A");
  });
  it("headline: a list reads `20 → 100 tickers`, a scalar `old → new`", () => {
    const twenty = Array.from({ length: 20 }, (_, i) => `T${i}`);
    const hundred = Array.from({ length: 100 }, (_, i) => `T${i}`);
    const ch = change({ key: "universe", old: twenty, new: hundred, is_list: true, group: "universe" });
    expect(changeHeadline(ch, "tickers")).toBe("20 → 100 tickers");
    expect(changeHeadline(change())).toBe("5 → 4");
    expect(changeHeadline(change({ is_default: true, new: null, new_text: null }))).toBe("5 → default");
    expect(changeHeadline(change({ is_default: true, new: null, new_text: "6" }))).toBe("5 → default (6)");
  });
  it("counts with the right noun", () => {
    expect(listNoun("tickers", 60)).toBe("60 tickers");
    expect(listNoun("tickers", 1)).toBe("1 ticker");
    expect(listNoun("user_ids", 2)).toBe("2 ids");
    expect(listNoun(null, 3)).toBe("3 items");
  });
});

describe("config sections", () => {
  const c = {
    groups: [
      { key: "account", label: "Account", keys: 1 },
      { key: "universe", label: "Universe", keys: 1 },
      { key: "risk", label: "Risk", keys: 2 },
    ],
    keys: [
      key("max_alloc_pct", "risk"),
      key("max_open_positions", "risk", { source: "override" }),
      key("universe", "universe", { is_list: true, value_type: "tickers" }),
      key("account_profile", "account", { value_type: "choice", choices: ["cash_long_only", "cash_debit", "margin"] }),
    ],
  } as unknown as Pick<OpsConfig, "keys" | "groups">;

  it("follows the API's registry group order, overrides first within a section", () => {
    const s = configSections(c);
    expect(s.map((x) => x.label)).toEqual(["Account", "Universe", "Risk"]);
    expect(s[2]!.keys.map((k) => k.key)).toEqual(["max_open_positions", "max_alloc_pct"]);
  });
  it("the page filter hides groups with no match", () => {
    expect(configSections(c, "alloc").map((x) => x.key)).toEqual(["risk"]);
    expect(configSections(c, "ACCOUNT_PRO").map((x) => x.key)).toEqual(["account"]);
    expect(configSections(c, "zzz")).toEqual([]);
  });
  it("overrides only", () => {
    expect(configSections(c, "", true).flatMap((x) => x.keys.map((k) => k.key))).toEqual(["max_open_positions"]);
  });
  it("a group missing from `groups` still shows, after the known ones", () => {
    const extra = { ...c, keys: [...c.keys, key("x.y", "newgroup")] };
    expect(configSections(extra).map((x) => x.key)).toEqual(["account", "universe", "risk", "newgroup"]);
  });
  it("keys wrap after each dot and filters match description too", () => {
    expect(keySegments("exits.vertical_credit.stop_value")).toEqual(["exits.", "vertical_credit.", "stop_value"]);
    expect(keyMatches(key("a", "risk"), "a description")).toBe(true);
  });
  it("allowed values: registry bounds, choices, list fallbacks", () => {
    expect(allowedText({ bounds: "0.5% – 10%", value_type: "float", choices: [] })).toBe("0.5% – 10%");
    expect(allowedText({ bounds: "a | b", value_type: "choice", choices: ["a", "b"] })).toBe("a | b");
    expect(allowedText({ bounds: "-", value_type: "tickers", choices: [] })).toBe("any tickers");
    expect(riskLabel("grow")).toBe("adding is riskier");
    expect(riskLabel("weird")).toBe("weird");
  });
});

describe("run detail helpers", () => {
  const ref = (kind: string, i: number, undeclared = false) => ({ id: `${kind}-${i}`, kind, subject: `s${i}`, undeclared, produced_by: null });
  it("counts context refs per kind, undeclared first, then by count", () => {
    const items = [
      ...Array.from({ length: 312 }, (_, i) => ref("raw_doc_ref", i)),
      ...Array.from({ length: 4 }, (_, i) => ref("candidate", i)),
      ref("note", 0),
      ref("proposal", 0, true),
    ] as StepView["read"];
    const c = kindCounts(items);
    expect(c.map((x) => [x.kind, x.n, x.undeclared])).toEqual([
      ["proposal", 1, true],
      ["raw_doc_ref", 312, false],
      ["candidate", 4, false],
      ["note", 1, false],
    ]);
    expect(kindCountsText(c)).toBe("proposal 1 · raw_doc_ref 312 · candidate 4 · note 1");
    expect(kindCountsText([])).toBe("none");
  });
  it("shortens hashes only", () => {
    const h = "0123456789abcdef0123456789abcdef01234567";
    expect(isHashLike(h)).toBe(true);
    expect(isHashLike("sha256:" + h)).toBe(true);
    expect(isHashLike("research")).toBe(false);
    expect(isHashLike("a b c d e f 0 1 2 3 4 5 6")).toBe(false);
    expect(shortHash(h)).toBe("0123456789…4567");
    expect(shortHash("short")).toBe("short");
  });
  it("keeps the last n log lines", () => {
    const lines = Array.from({ length: 120 }, (_, i) => i);
    expect(tailLines(lines, 50)).toHaveLength(50);
    expect(tailLines(lines, 50)[0]).toBe(70);
    expect(tailLines([1, 2], 50)).toEqual([1, 2]);
  });
});
