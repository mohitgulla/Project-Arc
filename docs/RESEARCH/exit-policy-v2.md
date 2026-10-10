# E18.3 · Exit policy v1 vs v2: backtest + live forward tally (D78)

Status: report only, flips nothing. Code: `scripts/exit_policy_report.py` (this report is its output), the shadow/MFE writer `arc journal backfill-exit-stats` (`arc/journal/outcomes.py`). Plan ref: D78, D19, E7.2/E7.5.

Reproduce (~5 min on 6 workers, offline on the cached chains; the live store is opened read-only):

    .venv/bin/python scripts/exit_policy_report.py \
        --data-dir ~/GitHub/Project-Arc/data \
        --db data/arc.db --json data/backtest/e183/results.json \
        --out docs/RESEARCH/exit-policy-v2.md --workers 6

## Verdict (rule fixed in the card before the run)

Keep v2 if, for `cash_debit`, v2's net P&L is not lower than v1's in ≥ 2 of 3 trend sub-periods **and** max drawdown is not higher by more than 10 %. Otherwise recommend rollback (or the best grid cell as a future XP arm, never a direct flip).

**KEEP v2: v2 net P&L not lower in 2 of 3 sub-periods (bear, sideways; need 2), max drawdown -36.3% vs v1 (limit +10%: ok).**

Margins: bear +12,228 for v2 (-44,291 vs -56,519), bull -1,204 (+27,708 vs +28,912), max drawdown 55,596 vs 87,340. Both variants lose money over the window (the E7.5 finding for `cash_debit`): v2 loses less, it does not make the profile profitable.

`cash_debit` does not trade sideways sessions (no neutral debit structure), so the sideways sub-period is 0 = 0 and always counts as "not lower": the rule really turns on bear and bull. The owner decides.

## Method

Run A of [ranking-backtest.md](ranking-backtest.md), narrowed: profile `cash_debit`, incumbent ranker `debit_width`, tickers SPY, QQQ, IWM, AAPL, NVDA, TSLA, daily decisions 2024-03-01 → 2026-07-31, entry DTE 30–60, `smile` marks, cost x = 0.25, 5000 MC paths, trend stance, D18 sizing, the pure gate caps. Exit variants (debit kinds):

- **v1**: take profit 1.00 × debit, no profit lock (before E18.1);
- **v2**: shipped E18.1 defaults: take profit 0.60 × debit, profit lock arms at a 0.50 × debit peak and closes at 0.20 × debit;
- **hold**: hold to expiry (no TP, stop, lock or DTE exit), reference.

Stop (0.75 × debit, end of day) and the 7-DTE exit are the same in v1 and v2. Each variant prices its own menus, because the managed-exit Monte Carlo (and the live managed Net EV > 0 floor) depends on the exit policy. Every rule is checked on end-of-day marks: the backtest has no intraday marks, so v2's intraday lock acts at the close here. **The fill-day guard (E18.2) is not modelled**: it holds discretionary (Research/LLM) closes, which the backtest does not simulate.

## Results (`cash_debit`, debit_width)

| Run | Trades | Net P&L | Max DD | Avg hold (d) | % by lock / TP / stop / DTE / expiry | P&L bear / sideways / bull | Lock-closed: realised vs hold-to-expiry |
|---|---|---|---|---|---|---|---|
| v1 | 118 | -27,608 | 87,340 | 19.6 | 0% / 40% / 49% / 8% / 3% | -56,519 / +0 / +28,912 | – |
| v2 | 164 | -16,583 | 55,596 | 15.0 | 6% / 50% / 38% / 4% / 1% | -44,291 / +0 / +27,708 | -2,958 vs -8,075 |
| hold | 68 | -30,678 | 73,499 | 42.9 | 0% / 0% / 0% / 0% / 100% | -56,009 / +0 / +25,330 | – |
| v1 exits on v2 picks | 132 | -26,673 | 71,036 | 19.7 | 0% / 38% / 50% / 10% / 2% | -53,516 / +0 / +26,844 | – |

"v1 exits on v2 picks" holds v2's menus and picks and changes only the exits: the difference to the v2 row is the exit rules alone, the difference to the v1 row is what the policy did to the menus (managed Net EV filter).

Hold-to-expiry P&L of the same picks: v1 -60,433, v2 -33,968.

## Threshold grid (descriptive only)

v2's menus and picks held fixed, each pick replayed under the cell's debit-kind exits (stop and DTE exit unchanged). Floor 0.0 = breakeven (the model needs floor > 0, so 1e-06 is used). A cell the exit-policy validator refuses (floor ≥ arm, or arm ≥ take profit: the lock could never act) is listed as n/a. **No cell is a recommendation to flip a default**: a cell worth trying goes into a future XP arm.

| Run | Trades | Net P&L | Max DD | Avg hold (d) | % by lock / TP / stop / DTE / expiry | P&L bear / sideways / bull | Lock-closed: realised vs hold-to-expiry |
|---|---|---|---|---|---|---|---|
| lock 0.3->0.0 tp 0.60 | 168 | -38,444 | 51,814 | 13.9 | 19% / 43% / 34% / 2% / 2% | -47,081 / +0 / +8,637 | -24,527 vs -23,976 |
| lock 0.3->0.1 tp 0.60 | 173 | -39,988 | 48,079 | 13.8 | 21% / 42% / 34% / 2% / 2% | -48,636 / +0 / +8,648 | -16,837 vs -5,713 |
| lock 0.3->0.2 tp 0.60 | 179 | -34,826 | 47,535 | 13.3 | 24% / 39% / 33% / 2% / 2% | -47,670 / +0 / +12,844 | -4,461 vs +44,620 |
| lock 0.3->0.3 tp 0.60 | n/a: profit_lock: floor_pct 0.3 must be < arm_pct 0.3 |  |  |  |  |  |  |
| lock 0.4->0.0 tp 0.60 | 165 | -19,410 | 55,139 | 15.0 | 10% / 50% / 36% / 2% / 2% | -46,015 / +0 / +26,605 | -13,405 vs -41,199 |
| lock 0.4->0.1 tp 0.60 | 167 | -20,544 | 52,176 | 14.8 | 11% / 49% / 36% / 2% / 2% | -47,275 / +0 / +26,731 | -7,420 vs -26,281 |
| lock 0.4->0.2 tp 0.60 | 172 | -18,363 | 53,412 | 14.5 | 15% / 46% / 35% / 2% / 2% | -47,646 / +0 / +29,283 | -1,944 vs +6,930 |
| lock 0.4->0.3 tp 0.60 | 175 | -16,202 | 55,150 | 13.9 | 17% / 44% / 36% / 2% / 1% | -42,035 / +0 / +25,833 | +5,262 vs +60,034 |
| lock 0.5->0.0 tp 0.60 | 162 | -15,440 | 55,596 | 15.3 | 5% / 51% / 38% / 4% / 1% | -46,449 / +0 / +31,009 | -5,468 vs -22,120 |
| lock 0.5->0.1 tp 0.60 | 163 | -15,370 | 55,596 | 15.1 | 6% / 51% / 38% / 4% / 1% | -44,692 / +0 / +29,322 | -4,030 vs -3,829 |
| lock 0.5->0.2 tp 0.60 | 164 | -16,583 | 55,596 | 15.0 | 6% / 50% / 38% / 4% / 1% | -44,291 / +0 / +27,708 | -2,958 vs -8,075 |
| lock 0.5->0.3 tp 0.60 | 164 | -15,751 | 54,638 | 15.0 | 7% / 49% / 38% / 4% / 1% | -45,880 / +0 / +30,129 | -1,041 vs +2,206 |
| tp 0.50 no lock | 167 | -20,837 | 51,115 | 14.7 | 0% / 54% / 40% / 5% / 1% | -47,453 / +0 / +26,616 | – |
| tp 0.50 lock 0.5->0.2 | n/a: profit_lock: arm_pct 0.5 must be < take_profit_pct_of_debit 0.5 |  |  |  |  |  |  |
| tp 0.60 no lock | 156 | -15,175 | 55,971 | 16.0 | 0% / 53% / 40% / 5% / 1% | -43,539 / +0 / +28,364 | – |
| tp 0.60 lock 0.5->0.2 | 164 | -16,583 | 55,596 | 15.0 | 6% / 50% / 38% / 4% / 1% | -44,291 / +0 / +27,708 | -2,958 vs -8,075 |
| tp 0.80 no lock | 137 | -27,465 | 63,800 | 18.9 | 0% / 43% / 47% / 9% / 1% | -56,196 / +0 / +28,731 | – |
| tp 0.80 lock 0.5->0.2 | 151 | -19,964 | 62,931 | 16.7 | 17% / 36% / 39% / 5% / 2% | -46,431 / +0 / +26,467 | -1,496 vs +6,735 |
| tp 1.00 no lock | 132 | -26,673 | 71,036 | 19.7 | 0% / 38% / 50% / 10% / 2% | -53,516 / +0 / +26,844 | – |
| tp 1.00 lock 0.5->0.2 | 150 | -14,958 | 70,294 | 16.9 | 18% / 34% / 41% / 5% / 2% | -54,711 / +0 / +39,753 | -1,575 vs -8,658 |

Best cell by net P&L: `tp 1.00 lock 0.5->0.2` (-14,958, max DD 70,294); descriptive, one sample, in-sample. Candidate for a future XP arm only.

## Live forward tally

Closed positions in the live store: 10; closed under v2 (after 2026-10-10T17:26:55Z, PR #201): **0**. The card's tally by exit reason (realised vs hold-to-expiry shadow) is appended once ≥ 20 positions have closed under v2.

Pre-v2 closes, for reference (MFE / MAE from the stored 30-min marks and the exit; shadow = hold-to-expiry P&L, priced by the nightly reconcile once the legs expire):

| Opened | Ticker | Kind | Exit reason | Realised | MAE | MFE | Peak % of debit | Shadow |
|---|---|---|---|---|---|---|---|---|
| 2026-10-01 | IWM | vertical_debit | remaining_ev_floor | -20 | -20 | +24 | 3% | – |
| 2026-10-01 | SPY | vertical_debit | remaining_ev_floor | -23 | -23 | +14 | 2% | – |
| 2026-10-01 | NFLX | vertical_debit | remaining_ev_floor | -70 | -70 | +0 | 0% | – |
| 2026-10-06 | VST | vertical_debit | research_review | +160 | -103 | +212 | 14% | – |
| 2026-10-07 | META | long_call | research_review | -75 | -250 | +167 | 4% | – |
| 2026-10-06 | CRWD | long_call | research_review | -935 | -951 | +110 | 6% | – |
| 2026-10-08 | TSM | vertical_debit | research_review | -90 | -90 | +0 | 0% | – |
| 2026-10-08 | HOOD | vertical_debit | research_review | -30 | -45 | +0 | 0% | – |
| 2026-10-06 | MRVL | vertical_debit | research_review | -410 | -462 | +45 | 4% | – |
| 2026-10-06 | INTC | vertical_debit | research_review | -662 | -662 | +0 | 0% | – |

Highest peak: 14% of the debit. v2's lock arms at 50 % and its take profit fires at 60 %, so neither v2 rule would have acted on any of these positions on the stored marks: their losses came from discretionary closes and the remaining-EV floor, not from the profit-taking rules.

## Caveats

Estimated spreads (no quotes), end-of-day decisions and marks only, the trend label is a deterministic proxy for Research's stance, 6 liquid names, one 29-month window. In-sample: the grid reuses the same window it describes. The live sample is far too small to prove anything.
