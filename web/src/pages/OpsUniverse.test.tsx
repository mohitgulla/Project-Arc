// E14.11 (D110): the pinned summary's pill + note tone per resolve state. `pending` (overnight,
// weekend, holiday before the Scalp's first resolve is due) is neutral; only a missed resolve warns.
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { Universe } from "../lib/universe";
import { UniverseSummary } from "./OpsUniverse";

const NOW = Date.parse("2026-10-14T06:36:00Z"); // 02:36 ET

function universe(state: Universe["state"], note: string | null): Universe {
  return {
    as_of: "2026-10-14T02:36:00-04:00",
    model: "d56",
    state,
    next_resolve_at: state === "pending" ? "2026-10-14T06:30:00-04:00" : null,
    resolved_for: state === "none" ? null : "2026-10-13",
    resolved_at: "2026-10-13T20:00:06-04:00",
    resolved_by: "scalp",
    age_s: 23_000,
    note,
    config_version: 3,
    active_max: 50,
    active: [],
    tiers: [],
    dropped: [],
    market_reference: ["SPY", "QQQ", "IWM"],
  } as unknown as Universe;
}

function attrs(html: string, testid: string): { tone: string | null; cls: string; text: string } {
  const re = new RegExp(`<[a-z]+([^>]*data-testid="${testid}"[^>]*)>([^<]*)<`);
  const m = re.exec(html);
  if (!m) throw new Error(`no ${testid} in ${html}`);
  const a = m[1]!;
  return { tone: /data-tone="([^"]*)"/.exec(a)?.[1] ?? null, cls: /class="([^"]*)"/.exec(a)?.[1] ?? "", text: m[2]! };
}

describe("UniverseSummary tone (E14.11)", () => {
  it("pending is neutral: muted pill and secondary note, no warn colour", () => {
    const html = renderToStaticMarkup(
      <UniverseSummary u={universe("pending", "Next resolve at 06:30 ET (Scalp). Until then consumers use the core list.")} now={NOW} />,
    );
    const pill = attrs(html, "uni-state");
    const note = attrs(html, "uni-note");
    expect(pill.tone).toBe("neutral");
    expect(pill.text).toBe("Awaiting Today&#x27;s Resolve · last 2026-10-13");
    expect(note.tone).toBe("neutral");
    expect(note.cls).toContain("text-secondary");
    expect(html).not.toContain("text-warn");
  });

  it("stale keeps the warn pill and warn note", () => {
    const html = renderToStaticMarkup(<UniverseSummary u={universe("stale", "Not resolved today: latest resolve is for 2026-10-13.")} now={NOW} />);
    const pill = attrs(html, "uni-state");
    const note = attrs(html, "uni-note");
    expect(pill.tone).toBe("warn");
    expect(pill.text).toBe("Not Resolved Today · last 2026-10-13");
    expect(note.cls).toContain("text-warn");
  });

  it("today is positive with no note", () => {
    const html = renderToStaticMarkup(<UniverseSummary u={universe("today", null)} now={NOW} />);
    expect(attrs(html, "uni-state").tone).toBe("pos");
    expect(html).not.toContain('data-testid="uni-note"');
  });
});
