# Bravos Research extraction guidelines

Version: `bravos-2026-10-04.1` (bump `guidelines_version` in `profile.yaml` whenever this file changes).

Written from "In 6 Months, It's All Over." (2026-09-29, 17 min) and "A Once in a Lifetime Wealth
Transfer Just Began." (2026-10-03, 22 min), English auto-captions. Bravos Research posts long-form
macro thesis videos about twice a week, in the afternoon ET. The format: one big theme (a foreign
market as a leading indicator, the Fed and liquidity, government debt, inflation as a "stealth
tax"), long historical analogues (2000, 2008, 2022), charts of margin debt, money supply and
moving averages, then the team's view of where the market goes over the **coming weeks to
months**. It closes with a paid-strategy pitch, which is stripped before you see the transcript.

Bravos almost never names a price level or a single-stock entry. Its value to Research is
the **regime view**: is the market's momentum intact, what macro force could break it, and when.

## What to extract

Extract only what the presenters **assert as their own view**. Skip:
- the history lessons (dot-com, 2008, 2022) except as the reason for a current view;
- what "mainstream media" or "headlines" say, unless the presenter agrees in his own words;
- the strategy's past performance ("we've been able to outperform the S&P 500"): that is
  marketing, not a view;
- anything left of the pitch (discounts, reports, spots left, links). If one slipped through,
  ignore it.

## Sections

- `market_bias`: the stance on US stocks (S&P 500 / Nasdaq) for the coming weeks. Bravos is often
  two-sided: a bearish macro thesis ("the bubble's final phase", "in 6 months it's all over")
  while saying momentum is "still strong, at least for now". In that case use `neutral`, or the
  thesis direction with `confidence` ≤ 0.4. Only a plain "we are reducing exposure / getting out"
  or "we are aggressively long" earns `confidence` ≥ 0.6.
- `catalysts`: scheduled or named macro events: Fed rate decisions and hikes, Treasury actions,
  debt ceiling, CPI/jobs data, a foreign-market sell-off they track as a lead indicator. Give a
  date only when stated; "in 6 months" or "in the coming months" is not a date.
- `risk_flags`: the warnings: leverage and margin debt, foreign institutional outflows, liquidity
  tightening, Fed hikes into rising inflation, wealth-gap / consumer stress, bubble valuations.
  `severity: high` only when they frame it as a bear market or a 20 %+ decline; a "correction"
  is `med`.

Macro claims go to `catalysts` or `risk_flags`, never to ticker calls. Do not create `levels` or
`calls` sections (they are not requested for this channel).

## Tickers

When a catalyst or risk names a market, use the ETF symbol in the text only: S&P 500 → SPY,
Nasdaq 100 → QQQ, small caps → IWM, gold → GLD, long treasuries → TLT. The South Korean KOSPI is
not a US ticker (EWY is the US ETF; mention it only if they do).

## Calibration

- "could", "might", "may", "potentially", "seems", "we happen to think", "if" → confidence ≤ 0.4.
- A stated allocation change by the team ("we've reduced our exposure") → confidence ≥ 0.6.
- Most of its views sit at or below 0.5: the theses are long-horizon and the timing is vague.

## Output

Strict JSON only, matching the schema in the prompt. Every item needs a `quote`: one contiguous,
verbatim excerpt of at most 240 characters from the transcript that supports the item. Do not
stitch sentences together or drop words from the middle. Code checks every quote against the
transcript and drops the item if it is not found. No sizing, no order language. When in doubt,
leave it out.
