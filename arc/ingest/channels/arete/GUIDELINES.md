# Arete Trading extraction guidelines

Version: `arete-2026-10-04.1` (bump `guidelines_version` in `profile.yaml` whenever this file changes).

Written from "Stock Market Breakout: Smart Money Is Buying, You're Not!" (2026-10-03 11:40 ET,
48 min, English auto-captions). Arete posts an analysis video most trading days around 17:00–18:00
ET, plus `PREMARKET LIVE` clips and morning streams (both excluded by the job config, never seen
here). The host works top-down — **index, then sector, then stock** — and talks a lot about options
positioning: put walls, call walls, gamma, and selling puts at the put wall instead of buying stock.
Large parts are recaps of his own trades (e.g. "I sold the 800 puts for today for 15").

## What to extract

Extract only what the host **asserts or plans going forward**. Skip:
- recaps of trades he already made and their P&L ("they just imploded to three. Yay.");
- analyst notes and articles he reads out, unless he states his own view on them;
- teaching asides about how options or market makers work.

Trading Floor / community plugs ("get on the wait list", "if you're in the community",
"links in description and it's pinned") are stripped before you see the transcript. If one slipped
through, ignore it.

## Sections

- `market_bias`: his overall stance on the index for the coming days.
- `levels`: put walls and call walls (`kind: support` / `resistance`), soft stops (`kind: stop`),
  and the levels he says he will act at (`kind: pivot`). Captions abbreviate ("at 80" can mean
  800 when he corrects himself): use the price he means, quote the caption text.
- `calls`: forward views on a ticker. Selling puts at a level is a **bullish** view; set
  `instrument_hint: none` (the hint names what a buyer would hold, and a short put is not a long
  put). `horizon: next_session` for "today / tomorrow", `swing` for weeks.
- `catalysts`: earnings, index rebalances, scheduled macro data, news that moved a name (kind
  `other`), with a date only when stated.
- `risk_flags`: his warnings (breadth illusion, rotation traps, a hard reject at a level).
  `severity: high` only when he frames it as a major drawdown.

## Tickers

Exchange symbols in capitals (STX, WDC, …). Index names map to ETFs: S&P 500 → SPY, Nasdaq → QQQ,
Russell → IWM. Sector names map to sector ETFs (semiconductors → SMH).

## Calibration

- "could", "might", "maybe", "watch", "I don't know", "if I get" → conviction ≤ 0.4.
- An explicit forward plan with a level ("right at those levels, I'm going to sell puts") →
  conviction ≥ 0.7.
- Everything in between: 0.4–0.7.

## Output

Strict JSON only, matching the schema in the prompt. Every item needs a `quote`: one contiguous,
verbatim excerpt of at most 240 characters from the transcript that supports the item. Do not
stitch sentences together or drop words from the middle. Code checks every quote against the
transcript and drops the item if it is not found. No sizing, no order language. When in doubt,
leave it out.
