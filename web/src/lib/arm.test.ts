import { describe, expect, it } from "vitest";

import {
  armDot,
  armIsViewable,
  bannerText,
  fallbackArmLabel,
  parseArm,
  searchWithArm,
  viewableArms,
  viewLabel,
  withArm,
  type ArmMeta,
} from "./arm";

const arm = (id: string, over: Partial<ArmMeta> = {}): ArmMeta => {
  const [xp, name] = id.split(":") as [string, string];
  return {
    id,
    experiment_id: xp,
    arm: name,
    label: `${xp} · ${name.toUpperCase()}`,
    status: "running",
    store_age_s: 30,
    viewable: true,
    ...over,
  };
};

const ARMS = [arm("XP-12:t1"), arm("XP-12:t2"), arm("XP-12:t3"), arm("XP-3:t1", { status: "stopped", viewable: false })];

describe("parseArm", () => {
  it("defaults to Production (null) without the param, empty or control", () => {
    expect(parseArm("")).toBeNull();
    expect(parseArm("?range=1W")).toBeNull();
    expect(parseArm("?arm=")).toBeNull();
    expect(parseArm("?arm=control")).toBeNull();
  });
  it("reads an arm id", () => {
    expect(parseArm("?arm=XP-12%3At1&range=1W")).toBe("XP-12:t1");
    expect(parseArm("?arm=XP-12:t2")).toBe("XP-12:t2");
  });
});

describe("searchWithArm", () => {
  it("sets the arm and keeps other params", () => {
    expect(searchWithArm("?range=1W", "XP-12:t1")).toBe("?range=1W&arm=XP-12%3At1");
  });
  it("removes the param for Production", () => {
    expect(searchWithArm("?range=1W&arm=XP-12%3At1", null)).toBe("?range=1W");
    expect(searchWithArm("?arm=XP-12%3At1", null)).toBe("");
  });
});

describe("withArm", () => {
  it("leaves Production queries unchanged (no arm key)", () => {
    expect(withArm({ range: "1D" }, null)).toEqual({ range: "1D" });
    expect(withArm(undefined, null)).toEqual({});
  });
  it("adds the arm to every query", () => {
    expect(withArm({ range: "1D" }, "XP-12:t1")).toEqual({ range: "1D", arm: "XP-12:t1" });
    expect(withArm(undefined, "XP-12:t1")).toEqual({ arm: "XP-12:t1" });
  });
});

describe("labels", () => {
  it("names the production account Production, never Control (D108)", () => {
    expect(viewLabel(null, ARMS)).toBe("Production");
    expect(viewLabel(null, undefined)).toBe("Production");
    expect(bannerText("XP-12 · T1")).toBe("Viewing XP-12 · T1 — virtual account, shared inputs from Production");
    expect(bannerText("XP-12 · T1")).not.toMatch(/control/i);
  });
  it("uses the API label, else derives XP-n · Tn", () => {
    expect(viewLabel("XP-12:t2", ARMS)).toBe("XP-12 · T2");
    expect(viewLabel("XP-7:t4", [])).toBe("XP-7 · T4");
    expect(fallbackArmLabel("XP-7:treatment")).toBe("XP-7 · T1");
    expect(fallbackArmLabel("odd")).toBe("odd");
  });
});

describe("viewable arms", () => {
  it("lists running arms only", () => {
    expect(viewableArms(ARMS).map((a) => a.id)).toEqual(["XP-12:t1", "XP-12:t2", "XP-12:t3"]);
    expect(viewableArms(undefined)).toEqual([]);
  });
  it("judges the URL's arm", () => {
    expect(armIsViewable(null, undefined)).toBe(true);
    expect(armIsViewable("XP-12:t1", undefined)).toBeUndefined();
    expect(armIsViewable("XP-12:t1", ARMS)).toBe(true);
    expect(armIsViewable("XP-3:t1", ARMS)).toBe(false);
    expect(armIsViewable("XP-99:t1", ARMS)).toBe(false);
  });
});

describe("armDot", () => {
  it("matches the Experiments page colours", () => {
    expect(armDot(null, ARMS)).toBe("var(--accent)");
    expect(armDot(ARMS[0]!, ARMS)).toBe("var(--series-2)");
    expect(armDot(ARMS[1]!, ARMS)).toBe("var(--series-1)");
  });
});
