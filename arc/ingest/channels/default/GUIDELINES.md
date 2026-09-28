# Default channel extraction guidelines

Version: `default-2026-09-27.1`.

Used for any YouTube channel that has no profile of its own under `arc/ingest/channels/<slug>/`.

- Extract only what the host **asserts or recommends**. Skip viewer questions, hypotheticals,
  quotes of other people's views, "some people think", past-trade recaps and shout-outs.
- Ignore sponsor, ad, course, Discord and "subscribe" segments.
- `market_bias`: the host's overall market stance, if stated.
- `levels`: price levels the host names on a ticker (support, resistance, pivot, target, stop).
- `calls`: directional views on a ticker, with the horizon the host implies.
- `catalysts`: scheduled data, earnings, Fed, sector and geopolitical events.
- `risk_flags`: market-wide warnings.
- Macro and geopolitical claims go to `catalysts` or `risk_flags`, never ticker calls.
- Index names map to ETFs: S&P 500 → SPY, Nasdaq → QQQ, Dow → DIA, Russell → IWM.
- Conviction: "could / might / watch" → ≤ 0.4; explicit "I'm buying / shorting" or a level with a
  plan → ≥ 0.7.
- Every item needs a verbatim `quote` (≤ 240 chars): one contiguous excerpt from the transcript,
  never sentences stitched together. Items whose quote is not in the transcript are dropped by code.
- Strict JSON only. No sizing, no order language. When in doubt, leave it out.
