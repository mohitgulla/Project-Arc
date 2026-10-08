# Investor's Business Daily extraction guidelines

Version: `ibd-2026-10-07.1` (bump `guidelines_version` in `profile.yaml` whenever this file changes).

Written from "Stocks Pare Losses As Yields Slash Gains; AbbVie, Apple, Micron In Focus | Stock
Market Today" (2026-10-07, 24 min, English auto-captions of the post-live stream). IBD's daily
show runs after the close (about 17:00 ET): two hosts walk the **major indexes** (Nasdaq, S&P
500, Dow, Russell 2000 and the equal-weight RSP), the **sector ETFs** that led or lagged (chips,
software, medical, financials), then three **stocks in focus** in CAN SLIM terms: bases, buy
points, handles, the 10/21/50-day lines, the relative-strength (RS) line, "three weeks tight".
Interviews and earnings previews appear on /videos two or three times a week. Product plugs
(MarketSurge, Leaderboard, IBD Live, the podcast, investors.com) are stripped before you see the
transcript.

## What to extract

Extract only what the hosts **assert as their own view** of what to do next. Skip:
- the recap of how each index or ETF closed today (that is market data, not a view), unless a
  host draws a conclusion from it ("breadth is weak", "the uptrend is intact");
- guest or analyst quotes the hosts read out without agreeing;
- disclosures ("I do own CrowdStrike") and programming notes (podcasts, cheat sheets,
  tomorrow's show).

## Sections

- `market_bias`: the stance on the market for the coming days, in IBD terms ("confirmed
  uptrend", "under pressure", "divided market"). A strong Nasdaq with weak breadth is a bullish
  bias with confidence ≤ 0.5.
- `levels`: buy points (`kind: pivot`), the 21-day / 50-day line a stock is testing
  (`kind: support`), a trend line or prior high it is trying to clear (`kind: resistance`). Use
  only prices they say out loud.
- `calls`: a forward view on a stock in focus. "Actionable", "you could have bought it here",
  "breaking out" → `bullish`; "getting rejected", "below the 50-day line, avoid" → `bearish`.
  `horizon: swing` (IBD trades over weeks); `instrument_hint: shares`.
- `catalysts`: earnings dates and pre-announcements, product launches, a peer's results that
  move the group (e.g. Samsung for memory), Treasury auctions and yields. Use kind `earnings`,
  `macro` or `sector`. Give a date only when stated.
- `risk_flags`: their warnings: weak breadth (small caps, equal-weight lagging), a sector
  reversal, leaders getting extended, yields rising. `severity: high` only when they say the
  market is in a correction or under pressure.

## Tickers

Exchange symbols in capitals. Captions spell symbols out letter by letter or mangle them
("ABV" for AbbVie is ABBV, "ARG"/"RG" for ARKG, "Madna" is Moderna MRNA): use the company's
real symbol only when the company is unambiguous. Index names map to ETFs: S&P 500 → SPY,
Nasdaq 100 → QQQ, Russell 2000 → IWM, equal-weight S&P → RSP; sectors to the ETF they name
(XLV, XBI, IGV, SMH).

## Calibration

- "could", "might", "maybe", "we'll have to see", "potentially", "arguably" → conviction ≤ 0.4.
- "Actionable today", "you could have bought it here today" with a named buy point or moving
  average → conviction 0.6–0.7.
- A view both hosts agree on can go to 0.7; nothing higher.

## Output

Strict JSON only, matching the schema in the prompt. Every item needs a `quote`: one contiguous,
verbatim excerpt of at most 240 characters from the transcript that supports the item. Do not
stitch sentences together or drop words from the middle. Code checks every quote against the
transcript and drops the item if it is not found. No sizing, no order language. When in doubt,
leave it out.
