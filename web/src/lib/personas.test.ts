import { describe, expect, it } from "vitest";

import { CAT } from "./personas.fixture";
import { EMPTY_CATALOGUE, categoryLabel, personaLabel, personaMeta, personaName } from "./personas";

describe("E13.14 persona catalogue (labels from /api/meta)", () => {
  it("leads with the catalogue emoji", () => {
    expect(personaLabel("research", CAT)).toBe("🧠 Research");
    expect(personaLabel("scout", CAT)).toBe("🔭 Scout");
    expect(personaLabel("broker", CAT)).toBe("🏦 Broker");
  });

  it("labels steps under their persona", () => {
    expect(personaLabel("risk.exit", CAT)).toBe("🛡️ Risk (exit)");
    expect(personaName("scalp.digest", CAT)).toBe("Scalp (digest)");
    expect(personaMeta("quant.open", CAT)?.key).toBe("quant");
  });

  it("shows the config label without an emoji for monitor", () => {
    expect(personaLabel("monitor", CAT)).toBe("Intraday monitor");
  });

  it("falls back to the capitalised key before meta loads or for unknown keys", () => {
    expect(personaLabel("research", EMPTY_CATALOGUE)).toBe("Research");
    expect(personaLabel("quant_pop", CAT)).toBe("Quant pop");
  });

  it("labels categories and the reference group", () => {
    expect(categoryLabel("market_news", CAT)).toBe("Market news");
    expect(categoryLabel("reference", CAT)).toBe("Reference data");
    expect(categoryLabel("company_data", CAT)).toBe("Company data");
  });
});
