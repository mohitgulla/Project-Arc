# E7.18 · Loss & drawdown anatomy — where, when and why the book loses (D84)

Report only (D84): nothing here changes a default, and no XP spec is drafted (D86). Every number below comes from a CSV in [`loss-anatomy/`](loss-anatomy/). The file name is given in brackets, and [`loss-anatomy/tables.md`](loss-anatomy/tables.md) renders all of the CSVs. Those CSVs hold aggregates only: there are no per-contract quote rows, which is what the D84 licence requires.

Reproduce (on main's sim, ≈ 22 min for C500 and ≈ 9 min for C125 at `--workers 6`):

```
nice -n 10 .venv/bin/python scripts/loss_anatomy.py runs --workers 6   # c500_dev + c125_dev books
.venv/bin/python scripts/loss_anatomy.py features                      # point-in-time entry table
.venv/bin/python scripts/loss_anatomy.py report                        # CSVs + tables.md
```

**Books.** Both books cover the dev period 2020-01-01 → 2023-12-31, with data running to 2024-03-30 so trades can settle. They use both profiles with the incumbent rankers (`credit_width` for margin, `debit_width` for cash_debit) and $25K of starting equity. The holdout was not read, because no `OWNER UPDATE: holdout ok` comment exists.

| | git | config hashes ranking / exits / costs / sim | build + walk |
|---|---|---|---|
| `c500_dev` (primary) | `ae3cf19` | `39d385e32c63` / `57e67df10186` / `1a72f97a1e20` / `6785cc9827c0` | 1294 s + 8 s |
| `c125_dev` (sign check) | `ae3cf19` | same | 535 s + 7 s |

C125 was re-run rather than reused from `attribution/fixed`. Its config hashes still match, but main moved after it (#316, D123, changed the liquidity screen's expiry choice). That changes which names pass on some days: the margin book now has 435 trades for −$22.9K, against the E7.17 book's 469 trades for −$17.4K.

Reconcile: Σ pnl = Δequity to the cent for all four books [`margin_reconcile`, `cash_debit_reconcile`].

| book | trades | net | end equity | max DD | win | per-$-risk | net at mid |
|---|---|---|---|---|---|---|---|
| margin C500 | 480 | −$17,739 | $7,261 | 78.7 % | 0.71 | −0.038 | −$547 |
| margin C125 | 435 | −$22,895 | $2,105 | 92.8 % | 0.69 | −0.053 | |
| cash_debit C500 | 674 | −$24,926 | $74 | 99.9 % | 0.49 | −0.039 | +$9,956 |
| cash_debit C125 | 485 | −$24,892 | $108 | 99.8 % | 0.51 | −0.068 | |

[`*_books`, `headline.json`]

**How to read the $ figures.** Risk per trade is a percentage of current equity, so the book compounds. A dollar lost in 2020 at $40K of equity counts for more than one lost in 2023 at $7K. Each table therefore also gives **per-$-risk** (net ÷ max loss) and **mean R**, which do not depend on size. "C125 flip" marks any bucket whose per-$-risk changes sign on C125.

---

## The 7-line answer

1. **Cost, not direction, for margin.** At mid the C500 margin book is flat at −$547. Slippage of $16,624 and fees of $569 make up the whole −$17.7K loss. For cash_debit, mid is **+$9,956** and slippage of $34,107 turns that into −$24.9K [`headline.json` cost]. E7.17 found the same on C125, so the result holds on the wider universe.
2. **Name/class: margin loses on ETFs and cash_debit loses on stocks.**
   - Margin ETFs lose −$16,335 of the −$17,739: broad equity −$7,882 (IWM −$5,340, SPY −$4,423) and commodity −$6,834 (GLD −$4,748).
   - Cash_debit stocks lose −$32,286 while its ETFs make +$7,360. Technology loses −$25,825 and communication −$20,667 (DIS −$15,989, AAPL −$15,569, MSFT −$13,194) [`*_by_etf_group`, `*_by_group`, `*_names_*_bottom_net`].
3. **Regime: losses cluster at regime turns.**
   - Margin entries made within 10 sessions before the SPY trend label flipped: n = 170 for **−$22,160** (per-risk −0.140). Every other entry made +$4,421 (+0.014). C125 agrees at −0.133 [`margin_by_transition`].
   - By cell, margin loses in SPY down × VIX 20–30 (−$9,555, n 56) and mixed × VIX 15–20 (−$7,731, n 51) [`margin_by_regime_cell`].
   - Cash_debit loses in down × VIX ≥ 30 (−$14,293, n 45; this is the March-2020 put cluster) and in up × VIX < 15 (−$15,892, n 187) [`cash_debit_by_regime_cell`].
4. **Trade type: the bleed is short-dated directional debit trades and condors.**
   - Cash_debit: long puts lose −$21,367 on n 31, of which long_put | down is −$20,758 on n 16. Bear puts lose −$13,627 (per-risk −0.298). Long calls are the only kind that earns, +$17,510 [`cash_debit_by_kind`, `cash_debit_by_kind_regime`].
   - Margin: iron condors lose −$11,380 and bull puts −$9,524. The bull-put loss sits in down/mixed tapes (−$5,520 and −$4,923). Bull puts in up tapes make +$919 [`margin_by_kind_regime`].
5. **Timeframe: losers declare themselves early, and 2021–22 is where the equity went.**
   - Margin trades held ≥ 29 sessions lose −$27,738 (n 123, mostly `dte_exit`) [`margin_by_hold_bucket`].
   - Cash_debit losers reach −0.5 R in a median of 5 sessions [`cash_debit_path_by_kind`].
   - By year, cash_debit 2021 lost −$42,908 and margin 2022 lost −$11,881 [`*_by_year`].
   - Margin entries within 2 sessions of FOMC/CPI lost −$13,408 on n 106 (−0.089 per-risk, against −0.013 for the rest; C125 −0.075) [`margin_by_macro_2s`]. Cash_debit entries on a ≥ 2 % gap day lost −$31,328 on n 110 (−0.234, against +0.013; C125 −0.194) [`cash_debit_by_gap_day`].
6. **Sizing/correlation: the book stacks correlated risk.**
   - In cash_debit's top-5 decline legs, 6–10 positions were open at the peak, all in one direction, carrying 35–75 % of equity at risk. Margin's crowded legs had 8–10 positions and 34–69 % [episode cards below].
   - Trades opened with the most same-cluster names already open did worse in both profiles. The top quintile is 2–6 names for margin and 1–6 for cash_debit; q5 − q1 = −0.166 R for margin and −0.198 R for cash_debit. The cash_debit result holds on C125 (−0.423 R); the margin one flips (+0.008) [`*_features_univariate`, `*_features_c125_sign`].
7. **Information gap: the features we have barely predict losers.**
   - The out-of-sample AUC of a shallow classifier is **0.586** for margin and **0.494** for cash_debit, pooled over time-ordered folds [`*_classifier_auc`]. Cash_debit losers are not predictable from entry-time EOD data.
   - The losses that missing information plausibly explains:
     - earnings held through by cash_debit stocks: −$22,318 on n 49;
     - entries before a regime flip: −$22,160 margin and −$10,873 cash_debit;
     - EOD stop overshoot: $24,967 cash_debit and $8,205 margin;
     - FOMC/CPI weeks: −$13,408 margin [`*_missing_info`].

---

## What's missing

Columns: the information, the evidence it matters (C500 dev), whether selection uses it today, whether it is available live, and a source with its cost.

| information | evidence it matters (C500 dev) | used today? | available live? | source · cost (read 2026-10-16) |
|---|---|---|---|---|
| **Real earnings calendar + implied-move awareness for debit trades** | Cash_debit stocks with a detected earnings day inside the hold: n 49, **−$22,318**, which is 69 % of the cash_debit stock loss. Margin: n 26, −$444 [`*_missing_info`]. The gate's `earnings_blackout` covers net-credit structures only (`arc/gate/rules.py` `check_earnings_blackout`), so a debit trade can hold through earnings and pay the IV crush. The backtest also only *infers* the event from the option-stats jump. | partly: credit side only (gate) | yes: Finnhub calendar already ingested (`raw_docs`) | Finnhub free tier: 1 month of calendar plus real-time. Historical calendar back to 20 years is on the $3,500/mo All-In-One plan. |
| **Market regime stance (news/LLM, faster than a 50/200-day label)** | Entries within 10 sessions before the SPY trend label flipped: margin n 170, **−$22,160**; cash_debit n 236, −$10,873 [`*_missing_info`, `*_by_transition`]. The backtest stance is the ticker's own trend label, and the live LLM stance cannot be replayed. | live: Research reads `market_health`; backtest: no | live: yes; backtest: no | No vendor fix. The test is a shadow/live comparison of the LLM stance against the label. |
| **Intraday path (stops fill at EOD)** | Total stop overshoot beyond −0.75 R: cash_debit **$24,967**, margin $8,205. Gap-through stops: cash_debit n 21, −$8,085; margin n 10, −$3,950 [`*_missing_info`, `*_path_by_kind`]. The mean overshoot is 0.08–0.13 R. | no (EOD sim) | live monitor is 5-min, so mostly yes | ThetaData Options Standard $80/mo (tick/NBBO) or Value $40/mo (1-min) for a backtest of intraday stops. |
| **Macro calendar in selection** | Margin entries with FOMC/CPI within 2 sessions: n 106, **−$13,408** (per-risk −0.089 against −0.013 for the rest, C125 −0.075). Cash_debit: n 94, −$1,142, flat [`*_by_macro_2s`]. A CPI release inside the first 2 sessions of a margin trade: n 84, −$15,661 [`margin_features_flags`]. | no | yes: static calendar, now committed (`config/backtest/macro_calendar_2020_2026.yaml`) | Free (Fed/BLS schedules). |
| **Correlation / cluster exposure** | Cash_debit q5 (≥ 1 same-cluster name open) − q1 = −0.198 R, CI [−0.377, −0.007], C125 −0.423. Margin −0.166 R, CI [−0.278, −0.047], but C125 flips to +0.008 [`*_features_univariate`, `*_features_c125_sign`]. | no: the gate caps open positions and Greeks, not correlation | yes: derivable from daily bars we already hold | Free (in-house). |
| **Spread as % of premium** | Cash_debit q5 (spread 29–160 % of premium) − q1 = −0.202 R, C125 −0.335. Margin per-$-risk falls with spread too (guard below) [`cash_debit_features_univariate`]. | partly: the liquidity screen gates `atm_spread_pct`, not the structure's own spread vs premium | yes | Free (quote already fetched). |
| Dealer positioning / GEX, order flow | Not testable here: we hold no history of it. | no | no | SpotGamma Standard $89/mo or Pro $129/mo (read 2026-10-16), with no API history on those tiers. Unpriced lead only. |
| Sector/industry live | The cash_debit loss concentrates in technology (−$25,825) and communication (−$20,667) [`cash_debit_by_group`]. | no | no (sector is only in this study's SEC SIC cache) | SEC submissions API, free. |

**Selection-ignored features that separate losers.** Univariate quintiles with a 90 % bootstrap CI on q5 − q1 that excludes 0 and the same sign on C125 [`*_features_univariate`, `*_features_c125_sign`]:

| profile | feature (use today) | q1 → q5 mean R | q5 − q1 | 90 % CI | C125 |
|---|---|---|---|---|---|
| margin | `mkt_spy_dist_ma200` (live context only) | −0.253 → +0.025 | +0.278 | [0.083, 0.457] | +0.228 |
| margin | `px_dd_from_high` (ignored) | −0.181 → +0.010 | +0.190 | [0.066, 0.310] | +0.208 |
| margin | `opt_vrp20` (ignored) | +0.046 → −0.129 | −0.175 | [−0.291, −0.052] | −0.088 |
| margin | `mkt_spy_term_slope` (live context) | −0.118 → +0.046 | +0.164 | [0.033, 0.300] | +0.104 |
| margin | `px_dist_ma200` (ignored) | −0.101 → +0.050 | +0.150 | [0.013, 0.293] | +0.213 |
| margin | `opt_front_over_iv30` (ignored) | +0.008 → −0.123 | −0.131 | [−0.262, −0.005] | −0.129 |
| cash_debit | `px_dist_ma20` (live context) | −0.259 → +0.108 | +0.367 | [0.165, 0.562] | +0.293 |
| cash_debit | `px_ret_20d` (selection) | −0.259 → +0.102 | +0.362 | [0.144, 0.567] | +0.269 |
| cash_debit | `spread_pct_premium` (selection) | +0.006 → −0.196 | −0.202 | [−0.358, −0.052] | −0.335 |
| cash_debit | `same_cluster_open` (ignored) | +0.012 → −0.186 | −0.198 | [−0.377, −0.007] | −0.423 |

How to read this table:
- Margin loses most when SPY is under its 200-day MA, the ticker is deep off its high, and vol risk premium or term inversion is high: selling premium into a stretched, falling tape.
- Cash_debit loses most on names that are *below* their 20-day MA at entry, and the bearish debit trades make up much of that (§4).
- The classifier adds little beyond these single variables. For margin, permutation importance tops out at 0.020 (`px_dist_ma200`) [`margin_classifier_importance`].

---

## Top-5 decline-leg cards

The margin book never regains its 2020-02-18 high. The whole dev period is one peak → trough episode: 78.7 % deep, trough 2023-04-13. Cash_debit's main episode runs from 2021-01-14 to a trough of 99.9 % on 2023-12-04 [`*_episodes`]. Both books are one episode each, so the cards below cover the separable **decline legs** inside it: a fall of more than 10 % from a local high, with the local high reset after a +10 % rally. They are ranked by $ lost [`*_slides`, `*_episode_attribution`, `headline.json` episode_cards].

### margin

| # | peak → trough | loss | open at peak (risk % eq) | P&L split | top kinds / names | SPY at peak → trough |
|---|---|---|---|---|---|---|
| 1 | 2020-02-18 → 2020-03-09 | −$9,386 | 8 (69 %) | held-through −$9,488 · opened in slide +$102 | bull put −$4,921, condor −$4,552 · QQQ −$3,142, SPY −$2,297, GLD −$2,156 | up, VIX proxy 0.12 → down, 0.49, breadth 0.68 → 0.12 |
| 2 | 2020-07-31 → 2020-10-12 | −$8,667 | 9 (40 %) | opened −$4,580 · held −$4,087 | condor −$5,799 · IWM −$4,533, SPY −$3,296 | up → up (label never flipped) |
| 3 | 2022-05-24 → 2022-09-06 | −$8,182 | 1 (12 %) | **opened in slide −$8,066** | bull put −$8,465 · EWZ −$2,391, CVX −$2,315 | down → down, SPY −17.5 % off high |
| 4 | 2022-04-20 → 2022-05-17 | −$7,865 | 10 (34 %) | held −$6,879 · opened −$986 | condor −$6,064 (all commodity: SLV −$3,377, GLD −$2,687) | mixed → down |
| 5 | 2021-11-19 → 2021-12-27 | −$7,200 | 10 (66 %) | held −$4,893 · opened −$2,307 | condor −$4,418 · IWM −$2,807, GDX −$2,139 | up → up |

Margin's legs fall into two kinds:
- **A crowded book held into a shock** (legs 1, 4, 5), with 8–10 names and 34–69 % of equity at risk.
- **Bullish short puts added while the tape was falling** (leg 3: 19 opened in the slide, −$8,066, during a 2022 down tape). This is the "no same-direction risk during a drawdown" guard below.

### cash_debit

| # | peak → trough | loss | open at peak (risk % eq) | P&L split | top kinds / names | SPY at peak → trough |
|---|---|---|---|---|---|---|
| 1 | 2020-09-21 → 2020-11-02 | −$23,107 | 6 (65 %), all bullish | **opened in slide −$20,506** | long call −$18,468 · TSLA −$4,000, QQQ −$3,225, SPY −$3,109 | mixed → mixed |
| 2 | 2021-02-09 → 2021-03-02 | −$22,710 | 10 (35 %), all bullish | held −$14,031 · opened −$8,679 | long call −$18,718 · PLTR −$6,764, MSFT −$4,295, PYPL −$4,083 | up → up |
| 3 | 2020-03-23 → 2020-03-26 | −$20,848 | 10 (75 %), all bearish | held −$20,848 | long put −$16,168 · BAC −$4,913, SPY −$4,114 | down, VIX 0.56 → down (the V-bottom) |
| 4 | 2020-09-02 → 2020-09-11 | −$18,682 | 8 (54 %), all bullish | held −$17,610 | long call −$18,549 · AAPL −$5,126, TSLA −$4,586, ZM −$4,050 | up → up |
| 5 | 2021-01-14 → 2021-01-29 | −$18,515 | 10 (39 %), all bullish | held −$12,130 · opened −$6,385 | long call −$18,440 · IWM −$5,365, BAC −$3,460 | up → mixed |

Every cash_debit leg is **one direction, 6–10 names, and 35–75 % of equity in long premium**. The worst sits at a turning point, such as long puts into the 2020-03-23 low, or calls at the Sept-2020 and Feb-2021 tops. The stance follows each ticker's own trend label. When the market turns, every name's label is still on the old side, so the book holds one large one-sided bet.

---

## 1–3 · Regime and timeframe (selected rows)

| cut | margin net (n, per-risk) | cash_debit net (n, per-risk) | C125 sign |
|---|---|---|---|
| label flips ≤ 10 sessions after entry | −$22,160 (170, −0.140) | −$10,873 (236, −0.057) | same |
| no flip | +$4,421 (310, +0.014) | −$14,053 (438, −0.031) | margin flips (−0.010) |
| SPY down × VIX 20–30 | −$9,555 (56, −0.156) | −$1,214 (95, −0.031) | cash_debit flips |
| SPY up × VIX < 15 | +$2,479 (173, +0.013) | −$15,892 (187, −0.098) | margin flips |
| FOMC/CPI ≤ 2 sessions | −$13,408 (106, −0.089) | −$1,142 (94, −0.010) | same |
| gap day (≥ 2 %) | +$3,316 (33, +0.093) | −$31,328 (110, −0.234) | same |
| hold 3–8 sessions | +$8,671 (67, +0.123) | +$75,944 (126, +0.670) | — |
| hold 15–29 sessions | −$5,122 (214, −0.027) | −$101,114 (251, −0.406) | — |
| hold ≥ 29 sessions | −$27,738 (123, −0.224) | −$26,330 (43, −0.663) | — |

[`*_by_transition`, `*_by_regime_cell`, `*_by_macro_2s`, `*_by_gap_day`, `*_by_hold_bucket`]

- Every trade enters at 31–45 DTE, the only DTE bucket the incumbent profile allows [`*_by_dte_bucket`].
- Day of week, cash_debit:
  - Friday entries lose −$25,708 (per-risk −0.243) and Tuesday entries make +$32,043 (+0.224).
  - The sign holds on C125 (−0.135 / +0.188), but there is no mechanism, and margin shows no such pattern.
  - Treat it as a lead to re-test, not a finding [`*_by_weekday`].
- By month, cash_debit's worst is March (−$25,250, n 72, mostly 2020) [`cash_debit_by_month`].
- **Is the loss in the regime the stance mapping assumes?**
  - Margin condors are the "neutral" stance, and they lose most in a SPY **down** tape (−$5,810, n 29, per-risk −0.220). Condors in an up tape lose only −$1,973 on n 135 [`margin_by_kind_regime`].
  - Bull puts are a "bullish" stance on the ticker's own label, and they lose −$10,443 in SPY down/mixed tapes [`margin_by_kind_regime`].
  - So the ticker label calls a name "sideways" or "bullish" while the market is trending against it.

## 4 · Trade-path anatomy

| | margin | cash_debit |
|---|---|---|
| losers ≥ 0.5 R | 106, −$74,163 | 304, −$245,922 |
| median sessions to −0.5 R (losers) | 13 | 5 |
| median MAE / MFE (R) | −0.13 / +0.13 | −0.58 / +0.42 |
| stops | 71, mean −0.88 R | 291, mean −0.83 R |
| stops that were ≥ +0.3 R first (gave back a winner) | 6 % | 14 % |
| stop overshoot vs −0.75 R: gap-through / drift | 0.16 / 0.12 R | 0.01 / 0.09 R |
| take-profit rate · median days to TP | 0.67 · 17 | 0.46 · 8 |

[`*_path_by_kind`]

**Would a "never green by day N" rule be visible at EOD?** Yes, but it doesn't pay:
- **Cash_debit.** Trades not green by session 3: n 236, final mean −0.380 R, a 68 % loser rate. Closing them at the session-3 EOD mark would give −0.378 R, about the same.
- **Margin.** Trades not green by session 5: −0.216 R held against −0.187 R at the session-5 mark [`*_never_green`].
- These losers are already lost by the time the signal shows. A time stop changes when the loss is booked, not its size.
- The stop works as designed, with 0.08–0.13 R of overshoot. The loss is in **what gets entered**, not in how it is exited.

## 5 · Names

- **Margin bottom names**:
  - by net: IWM (−$5,340, n 73), GLD (−$4,748, n 35), SPY (−$4,423, n 89), EWZ, GDX;
  - by per-risk: one-trade stock blow-ups (CSCO −1.07, ROKU −1.01, CVX −0.87).
  - IWM, GLD and SPY are **persistent**, losing across years. The stock losers are **episodic**, one bad trade each [`margin_names_*`].
- **Cash_debit bottom names**: DIS (−$15,989, n 9), AAPL (−$15,569, n 30), MSFT (−$13,194, n 25), then SPY and QQQ among ETFs. Of the bottom 25 stocks, 14 lose in more than one year [`cash_debit_names_*`].
- **Leveraged and commodity ETFs as their own groups**:
  - Leveraged is small in both books (n 3 / 7).
  - Commodity is margin's second-largest loss bucket (−$6,834, n 74, per-risk −0.124). Cash_debit commodity is +$2,493 on n 102 [`*_by_etf_group`].

## 7 · Calibration by regime

- Managed EV ranks P&L only weakly, and only in some slices [`*_calibration_slices`]:
  - Margin's best Spearman is 0.295 (VIX proxy 20–30), 0.279 (IV-rank q1) and 0.277 (VIX < 15). Its worst is ≈ 0 (IV-rank q4 −0.001, SPY mixed 0.000).
  - Cash_debit never exceeds 0.106 (IV-rank q3), and its worst slices are *negative*: IV-rank q4 −0.243, bear puts −0.191, ticker-trend bear −0.183.
- The bear slices are where the model's predicted per-unit EV is highest relative to realised P&L. For cash_debit ticker-bear entries, the model predicted +$29.4 per unit and the book realised −$110.0. Predicted PoP of 0.60 met a realised win rate of 0.40.

## 8 · Counterfactual guards (descriptive, same path, no CI; candidates for a BT study)

Each guard is one variable, walked over the C500 build. Blocked trades are removed and their freed slots are **not** refilled, so these are upper-bound sketches, not sims. Σ R is the size-free total [`*_guards`].

| guard | margin blocked · net · max DD · Σ R | cash_debit blocked · net · max DD · Σ R |
|---|---|---|
| baseline | 0 · −$17,739 · 78.7 % · −26.2 | 0 · −$24,926 · 99.9 % · −34.5 |
| skip SPY < 50-day MA and VIX-proxy pct > 80 | 62 · −$13,725 · 65.0 % · −17.6 | 92 · −$17,054 · 89.0 % · −21.3 |
| no same-direction risk during > 10 % DD | 109 · −$10,605 · 54.9 % · −16.2 | 480 · −$19,430 · 116.0 % · −22.0 |
| cluster cap 2 (60d corr > 0.7) | 132 · −$8,838 · 51.0 % · −15.1 | 104 · −$8,732 · 72.0 % · −11.7 |
| skip spread > 20 % of premium | 157 · −$7,520 · 50.7 % · −14.4 | 201 · −$6,341 · 72.4 % · **−2.2** |
| skip FOMC/CPI within 2 sessions | 106 · −$4,331 · **33.4 %** · −16.4 | 94 · −$23,784 · 98.2 % · −34.9 |
| open-risk cap 30 % of equity | 313 · −$20,761 · 86.7 % · −24.2 | 470 · +$3,232 · 95.3 % · −25.6 |

Reading the guards:
- **Spread > 20 %** and **cluster cap 2** improve Σ R in both profiles. The trades they block average −0.075/−0.084 R (margin) and −0.161/−0.219 R (cash_debit), below the book's −0.05.
- **FOMC/CPI** helps margin only.
- The **open-risk cap** turns cash_debit $ positive only through path effects: its blocked trades average −0.019 R, about the book average, and Σ R barely moves. Treat that $ as noise.
- The DD-direction guard blocks 480 of cash_debit's 674 trades, because that book sits in drawdown most of the time. It is not useful there.

---

## Ranked candidate changes (nothing applied)

Each candidate is a draft BT study for the nightly runner (E26). They all share one decision rule, D25 verbatim: *"beat the incumbent on net P&L **and** max drawdown in ≥2 of 3 sub-periods, with a 90% bootstrap CI excluding 0."* That covers net and max drawdown, sub-periods 2020–21 / 2022 / 2023 on C125 dev, then C500 dev, and holdout only after a win.

1. **Spread-to-premium cap at entry** (both profiles).
   - Evidence: cash_debit q5 − q1 −0.202 R, C125 −0.335; the guard takes Σ R from −34.5 to −2.2 by blocking 201, and from −26.2 to −14.4 for margin by blocking 157.
   - *Hypothesis:* skipping structures whose round-trip spread exceeds X % of premium raises per-$-risk without starving the book.
   - *Arms:* incumbent vs cap 20 % vs cap 30 %, with freed slots refilled by the next-ranked candidate.
2. **Correlation cluster cap** (both profiles).
   - Evidence: cash_debit same-cluster q5 − q1 −0.198 R (C125 −0.423); every cash_debit decline leg is 6–10 one-direction names.
   - *Hypothesis:* capping open names with a 60-day correlation > 0.7 at 2 cuts leg depth.
   - *Arms:* incumbent vs cap 2 vs cap 3.
3. **Debit earnings blackout** (cash_debit). Evidence: n 49, −$22,318, 69 % of the cash_debit stock loss.
   - *Hypothesis:* extending `earnings_blackout` to debit structures (no long premium held through a known event) stops the IV-crush losses.
   - *Arms:* incumbent vs blackout on debit too.
   - Needs a real historical earnings calendar in the backtest, which is a data lead. The live calendar is already ingested.
4. **Macro-event entry blackout** (margin). Evidence: n 106, −$13,408, per-risk −0.089 against −0.013, same sign on C125; the guard gives max DD 33.4 % against 78.7 %.
   - *Hypothesis:* skipping new margin entries with FOMC/CPI inside the first 2 sessions lowers drawdown.
   - *Arms:* incumbent vs skip ≤ 2 sessions vs skip ≤ 1 session.
   - The calendar is committed with this PR.
5. **Market-trend gate on short premium** (margin).
   - Evidence: `mkt_spy_dist_ma200` q5 − q1 +0.278 R, CI [0.083, 0.457], C125 +0.228; bull puts lose −$10,443 in SPY down/mixed tapes; the SPY < MA50 × VIX guard blocks 62 and moves Σ R from −26.2 to −17.6.
   - *Hypothesis:* condition the stance on SPY trend as well as the ticker label: no bull puts or condors when SPY < 200-day MA.
   - *Arms:* incumbent vs SPY-200 gate vs SPY-50 + VIX-pct gate.
6. **Data leads (no BT yet).**
   - Intraday stop path: $24,967 + $8,205 overshoot. ThetaData 1-min (Value, $40/mo) would test an intraday stop.
   - Regime-flip losses: −$22,160 margin. Compare the live LLM market stance in shadow against the trend label; no vendor needed.

The A/B rows hold best on the relative ranking of arms. The absolute $ path depends on compounding and on EOD fills.

## Caveats

- **EOD only.** Stops, take-profits and marks are EOD, so intraday reversals are invisible, and the overshoot figures are a lower bound on what an intraday stop would change.
- **Dev period only.** The holdout (2024 →) was not read.
- **C500 survivorship.** The C500 list is today's, and its ranks are point-in-time but its membership is not.
- **Stance proxy.** The stance is the ticker trend label, not the LLM personas (Scout/Research). The regime-transition finding is the main place where that matters.
- **Path dependence of $.** The book compounds, so per-$-risk and Σ R are the size-free measures, and the guards don't refill slots.
- **Inferred earnings.** Earnings events come from option-stats jumps, not a calendar. The macro calendar is the published schedule, with release dates as known before the event.
- **Sector and VIX inputs.**
  - Sector comes from SEC SIC with in-house groupings; ETFs are classed from the fund name.
  - The VIX proxy is SPY's `iv30` from option_stats, not VIX itself.
  - SPY's 200-day MA is undefined before mid-June 2020 because bars start 2019-09-03. Until then the trend label uses the 50-day MA alone.
