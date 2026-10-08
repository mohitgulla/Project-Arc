# Warrior Trading extraction guidelines

Version: `warrior-2026-10-07.1` (bump `guidelines_version` in `profile.yaml` whenever this file changes).

Written from "Did Short Sellers Just Lose $32 Million This Morning?" (2026-10-07, 16 min,
English auto-captions). Ross Cameron posts one recap per weekday around 09:40 ET: the
morning's **leading gainers** (mostly small caps, often low-float foreign listings), the trades
he took and their P&L, why a move happened (news or no news, float, volume, short squeeze),
and his outlook for the rest of the day and week. On Sundays he posts a "Day Trading Watch
List for Monday". The video closes with a Warrior Pro membership / trial pitch, which is
stripped before you see the transcript.

Most names he trades are sub-$10 small caps that will fail Arc's liquidity and price screen.
That is expected: extract them anyway when he states a forward view, and let code decide.

## What to extract

Extract only what he **asserts or plans going forward**. Skip:
- recaps of trades he already took and their P&L ("I made $50,000 on it", "stopped out for a
  loss"): a past trade is not a call;
- the play-by-play of a chart he is replaying (each candle, each price he lists while
  scrolling);
- teaching asides about how short sellers or market makers work, unless he ties them to a
  view on a named ticker;
- anything left of the pitch (Warrior Pro, the small cap room, a two-week trial, a link in the
  description, courses). If one slipped through, ignore it.

## Sections

- `market_bias`: only when he states a view on the broad market (SPY/QQQ/IWM) or on small-cap
  momentum as a whole for the rest of the day or week. "Momentum is weak / we're in between
  themes" is a `neutral` bias with low confidence; do not turn a single stock's move into a
  market bias.
- `levels`: a named ticker's high of day, flat-top breakout, VWAP or support he says he will act
  at (`kind: pivot` / `support` / `resistance`). Captions mangle prices ("$12030", "2 or$ 225"):
  use the price he means only when it is unambiguous, otherwise leave the level out.
- `calls`: a forward view on a named ticker ("I'm watching X for the curl through the high of
  day"). `horizon: intraday` for "today", `next_session` for "tomorrow". Day-trade longs are
  `instrument_hint: shares`.
- `catalysts`: the reason a name is moving: news / no news, an offering or shelf registration,
  a sector theme (space, pharma), an earnings or FDA headline. Use kind `other` for a stock-
  specific headline or a no-news squeeze, `sector` for a theme. Give a date only when stated.
  Float and volume belong in the `event` text ("float 1.3M shares, 22M volume").
- `risk_flags`: his warnings: no-news squeezes that give back their gains, short-squeeze
  blowups, thin support after a one-candle spike, dilution / offerings, choppy momentum.
  `severity: high` only when he frames it as account-threatening.

## Tickers

Exchange symbols in capitals (SXTC, BIYA, LGCL). The captions often misspell a symbol the
first time ("BYA" for "BIYA"); use the spelling he settles on, and only symbols he actually
reads out. Index names map to ETFs: S&P 500 → SPY, Nasdaq → QQQ, Russell / small caps → IWM.

## Calibration

- "could", "might", "maybe", "I don't know", "we'll see", "my hope" → conviction ≤ 0.4.
- A stated plan with a trigger ("if it curls back through 250 I'm in") → conviction 0.6–0.7.
- Momentum names are short-lived: nothing from this channel gets conviction above 0.7, and a
  call's horizon is never longer than `next_session`.

## Output

Strict JSON only, matching the schema in the prompt. Every item needs a `quote`: one contiguous,
verbatim excerpt of at most 240 characters from the transcript that supports the item. Do not
stitch sentences together or drop words from the middle. Code checks every quote against the
transcript and drops the item if it is not found. No sizing, no order language. When in doubt,
leave it out.
