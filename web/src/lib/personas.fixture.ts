import type { Catalogue } from "./personas";

/** The /api/meta catalogue as the server builds it from config/routines.yaml (E13.14). */
export const CAT: Catalogue = {
  personas: [
    { key: "scout", label: "Scout", emoji: "🔭", llm: true, group: "scout" },
    { key: "scalp", label: "Scalp", emoji: "⚡", llm: true, group: "scalp" },
    { key: "research", label: "Research", emoji: "🧠", llm: true, group: "trading_loop" },
    { key: "quant", label: "Quant", emoji: "📐", llm: true, group: "position_management" },
    { key: "risk", label: "Risk", emoji: "🛡️", llm: true, group: "trading_loop" },
    { key: "broker", label: "Broker", emoji: "🏦", llm: false, group: "post_market" },
    { key: "ops", label: "Ops", emoji: "⚙️", llm: false, group: "post_market" },
    { key: "monitor", label: "Intraday monitor", emoji: "", llm: false, group: "position_management" },
  ],
  categories: [
    { key: "market_news", label: "Market news", max_age_minutes: 360, weight: 1, feed: "scalp", reference: false },
    { key: "options_slow", label: "Options slow", max_age_minutes: 1440, weight: 1, feed: "scout", reference: false },
    { key: "reference", label: "Reference data", max_age_minutes: 0, weight: 0, feed: "scout", reference: true },
  ],
};
