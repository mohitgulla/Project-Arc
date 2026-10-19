# Candidate outcome ledger (E7.21, D125/D126)

One row per **menu candidate** per (session, ticker, profile, sleeve) that the backtest
sim builds, each labelled with the outcome it *would* have had under its sleeve's live
exits, plus the selection context that decided whether the sim could pick it. It is
the label/feature base for the E27 ML work and for loss and selection analysis; it
does not change the sim, the live loop or any strategy default.

- Code: `arc/ml/ledger/` (`build.py`, `outcomes.py`, `report.py`); CLI
  `arc ml ledger build|stats|report`.
- Data: `data/ml/ledger/v1/` (C500, 2020-01 → 2026-08, building at the time of
  writing); the C125 × 2021 measurement build is `data/ml/ledger_c125_2021/v1/`.
- Tables from the measurement build: [candidate-ledger/c125-2021.md](candidate-ledger/c125-2021.md)
  (regenerate with `arc ml ledger report --out <root> --doc <file>`).

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
per candidate, so the full ledger is the default. Projection for C500 × 2020-01 →
2026-08 (≈ 6.6 years, the larger names carry bigger menus): ≈ 4× rows per year ×
6.6 ≈ **90–100 M rows, ~22 GB, ~2–2.5 days of 6-worker compute** inside the
08:30–21:30 ET window (the build yields to the E26.3 runner and resumes from
per-(ticker, year) done markers).

## First reads (C125, 2021, dev only — descriptive, not a verdict)

- **Costs dominate the average candidate.** Mean `r_net` −0.03 at x = 0, −0.13 at
  x = 0.25, −0.20 at x = 0.5; win rate 50% → 46% → 41%. The cost share
  (`r_at_mid − r_net`) is largest on debit verticals and long premium.
- **Selection already helps:** candidates in some ranker's top-k (`ctx_in_topk`, 3.2%
  of rows) average −0.07 R vs −0.13 for all.
- **By kind (x = 0.25):** bull_put −0.01, long_call −0.01, iron_condor −0.06,
  bear_call −0.08, bull_call −0.15, long_put −0.16, bear_put −0.28 (2021 was a bull
  year; read per year once C500 lands).
- **Exit mix:** take-profit 42%, stop 20% (+16% intraday-proxy stop), expiry 11%,
  DTE exit 6%, locks 6%.
- **Rank IC (per-session Spearman vs r_net):** every stored key is positive within
  its pool; `rorc_day` is the strongest (0.08–0.21, t > 6 in every profile × sleeve),
  `managed_net_ev` 0.03–0.10. Caveat: `rorc_day` and `r_net` are both normalised by
  risk, so part of that IC is mechanical — an ML ranker must beat these per-pool ICs
  on the *same* pools, not the raw label.
- Context coverage: 19% of rows pass the entry filters (mid-range/stretched reasons
  recorded), 18% are in the sim's stance menu, 48% of name-sessions are on the active
  list; `ctx_regime_blocked` is non-zero only in sessions with SPY under its SMA
  (none in 2021; 11–26% of margin rows in H1 2020).

## Reading it

```python
from arc.ml.ledger import load_ledger
df = load_ledger("data/ml/ledger")          # dev rows only (D84)
df = load_ledger(root, split="holdout", allow_holdout=True, reason="E27.x final eval")
```

Holdout (sessions ≥ 2024-01-01) is refused unless `allow_holdout=True` **and** a
`reason` is given (logged). Weight rows by `sample_weight`, and de-weight overlapping
labels with `n_concurrent` (median 707 per ticker-profile window in C125: these are
heavily overlapping labels — use purged/embargoed CV by date).
