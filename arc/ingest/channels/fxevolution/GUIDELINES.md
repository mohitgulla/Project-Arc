# FX Evolution extraction guidelines

Version: `fxevolution-2026-10-04.1` (bump `guidelines_version` in `profile.yaml` whenever this file changes).

Written from "The World's Biggest Hedge Is Going Crazy..." (2026-10-01 23:44 ET, 26 min, English
auto-captions). FX Evolution posts a cross-asset market wrap most weeknights between about 23:00
and 01:00 ET. The host is a former institutional trader. The format: a headline macro theme (AI
capex, bonds, the consumer), dark-pool prints from "Volume Leaders", charts shared from X accounts
(Duality Research, Glassnode, Fairlead), breadth and risk-on/off ratios (S&P vs low-vol, NYSE
advance/decline, MOVE index, high-yield), then index, sector, Bitcoin, gold and FX charts. Calls are
mostly about **regime and direction over days to weeks**, rarely an entry for tomorrow.

## What to extract

Extract only what the host **asserts or recommends**. Skip:
- charts and opinions quoted from other people (X accounts, analysts) unless the host endorses
  them in his own words ("I think that's a very interesting point" is not an endorsement of a
  trade);
- rhetorical questions ("could we be going towards 10,000?") unless he answers them;
- dark-pool prints as *calls*: they describe someone else's order. Record them as a catalyst of
  kind `other` with `expected_impact: unknown` only when the host draws a conclusion from them;
- the history lessons (2018, 2022, the March lows) except as the reason for a current view.

Self-promotion is stripped before you see the transcript: free "live sessions" every couple of
weeks, the newsletter, "free in the description down below and pin comment", academy plugs and
"give them a follow". If one slipped through, ignore it.

## Sections

- `market_bias`: overall stance for the coming days. He is often two-sided ("calm before the
  storm, but … still making higher highs and higher lows"): use `neutral` or `confidence` ≤ 0.4
  in that case.
- `levels`: price levels he names on an index, ETF, Bitcoin (`BTC` is not a stock ticker: skip
  crypto levels unless an ETF such as `IBIT` is named), gold (`GLD`) or treasuries (`TLT`).
  He names few levels; don't invent them from "these points".
- `calls`: directional views with `horizon: swing` unless he says tomorrow (`next_session`) or
  months/years (`long_term`). Sector views map to the sector ETF (financials → XLF, tech → XLK,
  semiconductors → SMH, small caps / Russell → IWM, high yield → HYG, treasuries → TLT).
- `catalysts`: scheduled data and events he names (jobs, CPI, FOMC, earnings, a government
  shutdown), with a date only when stated.
- `risk_flags`: his warnings: weak breadth, MOVE spikes, high-yield stress, consumer weakness,
  AI-capex fatigue. `severity: high` only when he frames it as a crash or a 10 %+ decline.

Macro claims go to `catalysts` or `risk_flags`, never to ticker calls.

## Tickers

Exchange symbols in capitals. S&P 500 → SPY, Nasdaq / "the cues" (QQQ in captions) → QQQ,
Dow → DIA, Russell → IWM. Captions mangle names ("Ballinger" = Bollinger, "Kwave" = ratio,
"LOVAL" = low-volatility ETF SPLV, "the bow for high yield" = HYG). Write the real symbol and quote
the caption text as it appears.

## Calibration

- "could", "might", "maybe", "watching", "interesting", "suggest" → conviction ≤ 0.4.
- A plain "I'm buying / I'm out / I'm short", or a level with a plan, → conviction ≥ 0.7.
- Everything in between: 0.4–0.7. Most of his views sit at or below 0.5.

## Output

Strict JSON only, matching the schema in the prompt. Every item needs a `quote`: one contiguous,
verbatim excerpt of at most 240 characters from the transcript that supports the item. Do not
stitch sentences together or drop words from the middle. Code checks every quote against the
transcript and drops the item if it is not found. No sizing, no order language. When in doubt,
leave it out.
