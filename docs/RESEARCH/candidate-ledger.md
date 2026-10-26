# Candidate outcome ledger (E7.21, D125/D126)

One row per **menu candidate** per (session, ticker, profile, sleeve) that the backtest
sim builds, each labelled with the outcome it *would* have had under its sleeve's live
exits, plus the selection context that decided whether the sim could pick it. It is
the label/feature base for the E27 ML work and for loss and selection analysis; it
does not change the sim, the live loop or any strategy default.

- Code: `arc/ml/ledger/` (`build.py`, `outcomes.py`, `report.py`); CLI
  `arc ml ledger build|stats|report`.
- Data: `data/ml/ledger/v1/` (C500, 2020-01 → 2026-08: 55.6 M rows, 13.2 GB, built
  2026-10-19 → 10-20 at code `7bb2bddd`); the C125 × 2021 measurement build is
  `data/ml/ledger_c125_2021/v1/`.
- Tables: full C500 dev build [candidate-ledger/c500.md](candidate-ledger/c500.md);
  measurement build [candidate-ledger/c125-2021.md](candidate-ledger/c125-2021.md)
  (regenerate with `arc ml ledger report --out <root> --doc <file>`; it streams one
  profile × year partition at a time, so the C500 report takes ~4.5 min and peaks at
  4.7 GB RSS).

## What a row is

| group | columns |
|---|---|
| identity | `date, ticker, profile, sleeve, kind, direction, spec, key, legs, expiration, dte, split` |
| entry facts (decision close) | `spot, net_mid, credit, est_cost, max_loss_mid, max_gain_mid, width_pct, anchor_delta, atm_iv, rv_forecast, vrp, be_atr, be_sigma, spread_to_premium, leg_oi_min, leg_volume_min, shorts_sellable, bp_unit`, the ranker keys (`managed_net_ev, static_net_ev, managed_pop, static_pop, ev_proxy, rorc_day, credit_width, debit_width, expected_days_held`) and `rank_incumbent` |
| context (`ctx_*`) | `ctx_trend, ctx_vol` (the sim's proxy labels), `ctx_sim_menu` (in the stance menu the sim builds), `ctx_passes_filters` + `ctx_filter_reason` (anti-chase/range-edge verdict and why), `ctx_credit_only`, `ctx_in_topk` (in some ranker's top `per_name_depth` — the pool the cross-name walk picks from), `ctx_entry_spread_ok`, `ctx_earnings_blackout`, `ctx_regime_blocked` (D131 `short_premium_regime`), `ctx_eligible, ctx_screen_pass, ctx_seed, ctx_etf, ctx_liquidity`, and from `active.parquet` `ctx_screened, ctx_active, ctx_tier` |
| labels (`label_*`), per slippage x ∈ {0, 0.25, 0.5} | `pnl, r_net, r_hold, win, max_loss, mae_r, mfe_r` (`_x000/_x025/_x050`), plus fill-independent `r_at_mid, exit_reason, exit_date, end_date, days_held, hit_stop, hit_tp, hit_lock, exit_delays, crosses_holdout` |
| bookkeeping | `sample_weight` (inverse-probability, 1 unless `--sample-frac`), `n_concurrent` (same ticker × profile rows whose label window overlaps — for de-weighting overlapping labels) |

Features are **not** copied: join the E27.1 feature store on `(date, ticker)` (the
manifest records its schema hash).

## How labels are made (and why they equal the sim's)

Menus come from the sim's own functions (`sleeve_menus` → `pool_menus` →
`funnel_active`, extracted from `arc/backtest/sim.py` in this card, with the sim
re-pointed at them), built with `backtest.stance: none` so **every** structure and
direction is ledgered; the sim's own stance verdict is the `ctx_sim_menu` column.
The menu cache is shared with the sim.

Each candidate's exit path is walked **once** (mark-to-market on the exit book, stop/TP/
lock/DTE rules, intraday proxies, spread rule) and then **re-priced per x** through the
engine's `open_trade` / `close_early` / `settle` — the exit path does not depend on the
fill, so this is exact. All legs of a ticker share one `LegBook` (batched marks and
IV). Holds that span a split fall back to the sim's `sim_outcome`.

Verification:

- `tests/test_ml_ledger.py`: leg-sharing + one walk re-priced per x equals the naive
  per-candidate `sim_outcome` at every x (P&L, max loss, exit reason/day, hold P&L);
  the look-ahead mutation test (multiply every quote/close after d → entry facts and
  context unchanged, labels move); D84 holdout refusal; deterministic sampling;
  `n_concurrent`; report maths.
- Against the D125 baseline run (`baseline_d125_post128/c125_dev`), every one of the
  sim's **454 trades in 2021** (margin + cash_debit, `managed_net_ev`) is found in the
  ledger with the same P&L at x = 0.25 (± $0.01) and the same exit reason (454/454).
- The ledger's screened/active list counts equal the sim's `decisions.csv` on all 252
  sessions of 2021.

## Measurement (C125 × 2021, 6 workers, M-series Mac mini)

| | |
|---|---|
| rows | 3,724,538 (margin 1.94M · cash_debit 1.22M · cash_long_only 0.56M) |
| size | 0.88 GB parquet, ≈ 236 B/row, 91 columns |
| wall | 63 min (≈ 30 ticker-s/ticker-year; menu cache warm from the baseline) |
| null labels | 0 |
| sampled (2 tickers, 2021) | full 86k rows / 194 s; `--sample-frac 0.25` 64k / 307 s (cache cold); 0.1 → 59k / 175 s |

Off-band sampling saves little: most rows are in the live Δ bands, and the walk is
per candidate, so the full ledger is the default. The pre-build projection for C500 ×
2020-01 → 2026-08 was 90–100 M rows, ~22 GB and ~2–2.5 days of 6-worker compute.

## Measurement (C500 × 2020-01 → 2026-08, the full build)

| | |
|---|---|
| rows | 55,643,175 (margin 28.1 M · cash_debit 18.3 M · cash_long_only 9.3 M); dev 33.3 M (2020–23), holdout 22.3 M (2024 → 2026-08) |
| per year | 2020 7.1 M · 2021 8.4 M · 2022 8.9 M · 2023 9.0 M · 2024 8.1 M · 2025 8.8 M · 2026 (to Aug) 5.4 M |
| per sleeve / kind | short 35.5 M · swing 20.1 M; long_call 15.1 M, long_put 12.7 M, bear_put 9.5 M, bull_call 8.5 M, bull_put 3.9 M, iron_condor 3.1 M, bear_call 2.8 M |
| size | 13.24 GB parquet (≈ 238 B/row), 10,500 part files (882 empty: no menu for that ticker × year × profile) |
| build compute | 17.7 h summed over the yearly passes, 6 workers, `nice 10`; wall 2026-10-19 07:00 → 10-20 17:49 PT including the 21:30–08:30 ET yields |
| null labels | 0.07 % of rows (23,470 dev rows): candidates the sim's `sim_outcome` also returns `None` for (entry not priceable, or a non-whole-number adjustment / delisting inside the hold). Concentrated in leveraged/volatility ETPs with reverse splits (VXX, UVXY, SQQQ, SOXS, TZA, SPXU, USO), reverse-split or spun-off names (GE) and names acquired mid-hold (APHA, XLNX, LVGO, AGN). Drop them when training |

So the projection overshot by ~1.7× on rows and size, most likely because the C500's
smaller names list fewer expiries and strikes than the C125 average. No sampling was
needed (13 GB ≪ 40 GB), so the manifest records `n = 1` and every `sample_weight` is 1.

## First reads (C500 dev, 2020–23 — descriptive, not a verdict)

Full tables, all split All / Stocks / ETFs: [candidate-ledger/c500.md](candidate-ledger/c500.md).

- **Costs dominate the average candidate.** Margin mean `r_net` −0.016 at x = 0,
  −0.131 at x = 0.25, −0.215 at x = 0.5; win rate 55% → 50% → 42%. The cost share at
  x = 0.25 is 0.24–0.27 R on debit verticals, 0.06–0.09 R on credit kinds and
  0.03–0.04 R on long premium. ETFs pay the same or slightly less than stocks on
  every kind.
- **Selection already helps:** rows in some ranker's top-k (`ctx_in_topk`, 2.5% of
  margin rows) average −0.047 R vs −0.131 for all; rows that pass the entry filters
  −0.063; rows in the sim's stance menu −0.114 (the stance proxy adds little).
- **By kind (margin, x = 0.25, 2020–23):** long_call −0.03, bull_put −0.06,
  bear_call −0.07, long_put −0.11, iron_condor −0.11, bull_call −0.25, bear_put
  −0.27. Debit verticals are the worst kind in every year — the same finding as
  E7.19's attribution grid (the D128 debit kinds sink the margin book). Credit
  verticals win 62–74% of the time (iron condors 54–60%) but give back on the losers.
- **Exit mix (margin):** take-profit 45%, stop 17% (+12% intraday-proxy stop),
  expiry 13%, DTE exit 9%, locks 5%.
- **Rank IC (per-session Spearman vs r_net, 1,006 sessions):** `rorc_day` is the
  strongest key in every profile × sleeve (0.17–0.21 on margin and cash_debit, 0.06
  on cash_long_only, t > 9). On the margin menu `credit_width` is next (0.10 short,
  0.15 swing) and `managed_net_ev` — the live rank key — is weak (0.027 short, 0.062
  swing); `ev_proxy` is zero on margin short. Same pattern on Stocks and ETFs.
  Caveat: `rorc_day` and `r_net` are both normalised by risk, so part of that IC is
  mechanical — an ML ranker must beat these per-pool ICs on the *same* pools, not
  the raw label.

### First reads (C125, 2021 — the measurement build)

- Mean `r_net` −0.03 at x = 0, −0.13 at x = 0.25, −0.20 at x = 0.5; win rate
  50% → 46% → 41%.
- Top-k rows (3.2%) average −0.07 R vs −0.13 for all.
- **By kind (x = 0.25):** bull_put −0.01, long_call −0.01, iron_condor −0.06,
  bear_call −0.08, bull_call −0.15, long_put −0.16, bear_put −0.28 (2021 was a bull
  year).
- **Exit mix:** take-profit 42%, stop 20% (+16% intraday-proxy stop), expiry 11%,
  DTE exit 6%, locks 6%.
- **Rank IC:** `rorc_day` 0.08–0.21 (t > 6 in every profile × sleeve),
  `managed_net_ev` 0.03–0.10.
- Context coverage: 19% of rows pass the entry filters (mid-range/stretched reasons
  recorded), 18% are in the sim's stance menu, 48% of name-sessions are on the active
  list; `ctx_regime_blocked` is non-zero only in sessions with SPY under its SMA
  (none in 2021; 11–26% of margin rows in H1 2020).

## Reading it

```python
from arc.ml.ledger import load_ledger
from arc.ml.ledger.build import iter_ledger
df = load_ledger("data/ml/ledger")          # dev rows only (D84)
df = load_ledger(root, split="holdout", allow_holdout=True, reason="E27.x final eval")
for profile, year, part in iter_ledger("data/ml/ledger", columns=[...]):
    ...                                     # bounded memory: one profile × year at a time
```

Dev alone is 33 M rows × 91 columns, more than the host's 16 GB holds at once: read
with `columns=` and `profiles=`/`years=`, or stream with `iter_ledger`. The `split`
filter applies even when `columns=` leaves `split` out.

Holdout (sessions ≥ 2024-01-01) is refused unless `allow_holdout=True` **and** a
`reason` is given (logged). Weight rows by `sample_weight`, and de-weight overlapping
labels with `n_concurrent` (median 707 per ticker-profile window in C125: these are
heavily overlapping labels — use purged/embargoed CV by date).
