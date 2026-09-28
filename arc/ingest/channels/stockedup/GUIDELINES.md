# StockedUp extraction guidelines

Version: `stockedup-2026-09-27.2` (bump `guidelines_version` in `profile.yaml` whenever this file changes).

StockedUp posts one video almost every trading day, usually between 16:00 and 22:00 ET, about the
**next session**. The format is steady: macro and news recap, SPY levels, the week's economic and
earnings calendar, "setups and predictions", "momentum plays" with breakout levels for tomorrow,
and a "big money trade" (an unusual options flow print), with sponsor and Discord reads in between.

## What to extract

Extract only what the host **asserts or recommends**. Skip:
- viewer questions and comments, "some people think", quotes of other people's opinions that the
  host does not endorse;
- hypotheticals ("if X happened, Y might…") unless the host turns them into a plan;
- past performance recaps ("made $1,500 on Microsoft calls") and member shout-outs;
- the "big money trade" as a *call*: it describes someone else's order flow. You may record its
  ticker as a catalyst of kind `other` with `expected_impact: unknown` only if the host adds a view.

Sponsor and ad reads (Bookmap, Moomoo, "use code BIGMONEY", course or Discord plugs, "subscribe")
are stripped before you see the transcript. If one slipped through, ignore it.

## Sections

- `market_bias`: the host's overall stance for the next session. `confidence` ≤ 0.4 when the
  host is mixed ("close to highs but resistance is strong").
- `levels`: price levels the host names, mostly SPY / QQQ / IWM support and resistance, plus
  breakout / breakdown levels on momentum plays (`kind: pivot`) and targets. `price` is the number
  the host says, as a float. Auto-captions drop decimal points ("7550" usually means 75.50,
  "25140" usually means 251.40): write the price you believe the host meant, and quote the
  caption text as it appears.
- `calls`: directional views on a ticker. `horizon`: `next_session` for "tomorrow" / momentum
  plays, `swing` for "coming weeks", `long_term` for months or more. `instrument_hint` only if the
  host names one (calls, puts, spread, shares); otherwise `none`.
- `catalysts`: scheduled data (CPI, PPI, jobs, GDP, FOMC; kind `macro` or `fed`), earnings (kind
  `earnings`, with the ticker and date when the host gives them), sector and geopolitical events.
  Give `date` as `YYYY-MM-DD` only when it is stated or clearly implied by the calendar in the
  video; otherwise `null`.
- `risk_flags`: market-wide warnings (yield spikes, weak breadth, bubble talk, war escalation).
  `severity: high` only when the host frames it as a crash or major-drawdown risk.

Macro and geopolitical claims go to `catalysts` or `risk_flags`. **Never** turn them into ticker
calls.

## Tickers

Use exchange symbols in capitals. Index names map to ETFs: S&P 500 / "the market" → SPY,
Nasdaq → QQQ, Dow → DIA, Russell → IWM. Auto-captions often split symbols ("CO IN" is COIN,
"CR AK" / "CRK" is CRAK): write the real symbol, quote the caption text.

## Calibration

- "could", "might", "watch", "on the radar", "if and only if it breaks" → conviction ≤ 0.4.
- An explicit "I'm buying / shorting / adding", or a level with a plan (entry and stop or
  target), → conviction ≥ 0.7.
- Everything in between: 0.4–0.7.

## Output

Strict JSON only, matching the schema in the prompt. Every item needs a `quote`: one contiguous,
verbatim excerpt of at most 240 characters from the transcript that supports the item. Do not
stitch separate sentences together or drop words from the middle; pick the single sentence that
carries the claim. Code checks every quote against the transcript and drops the item if it is not
found. No sizing, no order language. When in doubt, leave it out.
