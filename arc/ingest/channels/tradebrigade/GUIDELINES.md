# Trade Brigade extraction guidelines

Version: `tradebrigade-2026-10-04.1` (bump `guidelines_version` in `profile.yaml` whenever this file changes).

Written from "New All Time Highs... Will It Last?!" (2026-10-03 20:49 ET, 70 min, English
auto-captions). Trade Brigade posts a long SPY / QQQ technical game plan on Wednesday evenings and
at the weekend (50–77 min). The format is steady: the weekly chart (anchored VWAPs, volume
profile), **this week's expected move** (upper and lower bound), the daily key level for buyers,
then hourly "pathing" (higher lows, inverted head and shoulders, gap fill), market internals and
breadth, then sectors and a few single names. Videos are dense with exact levels and conditional
plans.

## What to extract

Extract only what the host **asserts or plans**. Skip:
- the tutorial asides ("if you're not familiar with the expected move …");
- recaps of what happened last week, unless they set a level for this week;
- hypothetical shapes he draws and then rejects ("this would technically start looking like a
  lower high … that's just not really what we want to see").

Self-promotion is stripped before you see the transcript (the "video tutorial in the top right
hand corner" asides, "additional resources … in the description"). If one slipped through, ignore
it.

## Sections

- `market_bias`: his base case for the coming sessions. He states it as a condition ("the base
  case is we need to stay over 766"): stance `bullish` above the key level, with
  `confidence` ≤ 0.6 because it is conditional.
- `levels`: the levels he names, mostly SPY:
  - the expected-move upper and lower bounds (`kind: resistance` / `support`);
  - the key buyer level (`kind: support` or `pivot`);
  - breakout shelves (`kind: resistance`) and the all-time-high target (`kind: target`).
  Auto-captions drop the decimal point ("77935" means 779.35, "77485" means 774.85, "76875" means
  768.75). Write the price you believe he meant and quote the caption text as it appears.
- `calls`: SPY / QQQ / sector / stock views. `horizon: next_session` for "early stages of this
  week" plans, `swing` for weeks. `instrument_hint` only when he names one.
- `catalysts`: scheduled data (CPI, PPI, jobs, FOMC), earnings, index rebalances, with a date only
  when stated.
- `risk_flags`: weak breadth, a failed retest, a lower high on the daily. `severity: high` only
  when he frames it as a larger breakdown.

## Tickers

Exchange symbols in capitals. S&P 500 / "the market" → SPY, Nasdaq → QQQ, Russell → IWM,
"MAG 7" is not a ticker (skip it as a call; it can be a risk flag).

## Calibration

- "could", "might", "maybe", "ideally", "potentially", "if we could" → conviction ≤ 0.4.
- A level with a plan (trigger, then target) → conviction ≥ 0.7.
- Everything in between: 0.4–0.7.

## Output

Strict JSON only, matching the schema in the prompt. Every item needs a `quote`: one contiguous,
verbatim excerpt of at most 240 characters from the transcript that supports the item. Do not
stitch sentences together or drop words from the middle. Code checks every quote against the
transcript and drops the item if it is not found. No sizing, no order language. When in doubt,
leave it out.
