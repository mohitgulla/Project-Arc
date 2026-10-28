// /api/ml/* bodies. ML_MODELS, M1_DETAIL and DEBIT_DETAIL are real: read from data/ml on 2026-10-28.
// ML_STUDIES, ML2_DETAIL, ML5_DETAIL and SHADOW_DETAIL are the synthetic studies/health setup of
// tests/test_tower_ml.py (no ML-<n> has run live yet). No imports: e2e/models.spec.ts serves
// these bodies too, and the e2e tsconfig must not pull in the app sources.

export const ML_MODELS = {
 "as_of": "2026-10-28T09:00:00Z",
 "summary": {
  "champions": 1,
  "families": 3,
  "champion_families": [
   "m1"
  ],
  "in_shadow": 0,
  "studies_proposed": 0,
  "studies_queued": 0,
  "live_calibration": null,
  "live_calibration_n": null,
  "worst_psi": null,
  "worst_psi_feature": null,
  "health_day": null,
  "running": null,
  "queued": 0,
  "window_start": "21:30"
 },
 "families": [
  {
   "family": "m1",
   "label": "M1 · Move & Vol",
   "champion": "m1-53833784f276",
   "counts": {
    "champion": 1,
    "retired": 1
   },
   "holdout_reads": 0
  },
  {
   "family": "credit",
   "label": "M2 · Candidate Outcome (Credit)",
   "champion": null,
   "counts": {
    "candidate": 1
   },
   "holdout_reads": 0
  },
  {
   "family": "debit",
   "label": "M2 · Candidate Outcome (Debit)",
   "champion": null,
   "counts": {
    "candidate": 1
   },
   "holdout_reads": 0
  }
 ],
 "items": [
  {
   "family": "m1",
   "label": "M1 · Move & Vol",
   "family_kind": "quantile",
   "purpose": "Calibrated quantiles of each name's log return and realised vol over the next 5 to 42 sessions. Feeds the candidate model and the Monte Carlo path source.",
   "version": "m1-53833784f276",
   "status": "champion",
   "created": "2026-10-18T02:05:04.618595Z",
   "source": "card",
   "rule": "m1_keep",
   "rule_passed": null,
   "holdout_reads": 0,
   "stats": [
    {
     "label": "Keep Rule",
     "value": 0.0,
     "of": 8,
     "unit": "count",
     "text": null,
     "sub": "baseline kept",
     "tone": "neutral"
    },
    {
     "label": "Beats Baseline",
     "value": 4.0,
     "of": 8,
     "unit": "count",
     "text": null,
     "sub": "pinball, both years",
     "tone": "neutral"
    },
    {
     "label": "Coverage Error",
     "value": 0.5617753847423361,
     "of": null,
     "unit": "pts",
     "text": null,
     "sub": "shipped, vol h42 · 2023",
     "tone": "neg"
    }
   ]
  },
  {
   "family": "credit",
   "label": "M2 · Candidate Outcome (Credit)",
   "family_kind": "outcome",
   "purpose": "P(win), E[R], P(stop), R quantiles and expected hold per credit candidate, calibrated out of fold.",
   "version": "m2-05fa82807a93",
   "status": "candidate",
   "created": "2026-10-26T16:51:52.808075-04:00",
   "source": "card",
   "rule": "m2_promote",
   "rule_passed": false,
   "holdout_reads": 0,
   "stats": [
    {
     "label": "Promote Rule",
     "value": 0.0,
     "of": 2,
     "unit": "count",
     "text": null,
     "sub": "test years passed",
     "tone": "neutral"
    },
    {
     "label": "Log-Loss vs Logistic",
     "value": 0.042415,
     "of": null,
     "unit": "pct",
     "text": null,
     "sub": "win, 2023",
     "tone": "neg"
    },
    {
     "label": "E[R] IC",
     "value": 0.1372100147980891,
     "of": null,
     "unit": "ratio",
     "text": null,
     "sub": "per day, 2023",
     "tone": "pos"
    }
   ]
  },
  {
   "family": "debit",
   "label": "M2 · Candidate Outcome (Debit)",
   "family_kind": "outcome",
   "purpose": "The same targets for debit candidates.",
   "version": "m2-04e58916e118",
   "status": "candidate",
   "created": "2026-10-26T16:51:52.808075-04:00",
   "source": "card",
   "rule": "m2_promote",
   "rule_passed": false,
   "holdout_reads": 0,
   "stats": [
    {
     "label": "Promote Rule",
     "value": 0.0,
     "of": 2,
     "unit": "count",
     "text": null,
     "sub": "test years passed",
     "tone": "neutral"
    },
    {
     "label": "Log-Loss vs Logistic",
     "value": 0.066051,
     "of": null,
     "unit": "pct",
     "text": null,
     "sub": "win, 2023",
     "tone": "neg"
    },
    {
     "label": "E[R] IC",
     "value": 0.14606757784487695,
     "of": null,
     "unit": "ratio",
     "text": null,
     "sub": "per day, 2023",
     "tone": "pos"
    }
   ]
  }
 ],
 "scope": {
  "arm_id": null
 },
 "shared_from_control": false
};

export const M1_DETAIL = {
 "detail": {
  "as_of": "2026-10-28T09:00:00Z",
  "model": {
   "family": "m1",
   "label": "M1 · Move & Vol",
   "family_kind": "quantile",
   "purpose": "Calibrated quantiles of each name's log return and realised vol over the next 5 to 42 sessions. Feeds the candidate model and the Monte Carlo path source.",
   "version": "m1-53833784f276",
   "status": "champion",
   "created": "2026-10-18T02:05:04.618595Z",
   "source": "card",
   "rule": "m1_keep",
   "rule_passed": null,
   "holdout_reads": 0,
   "stats": [
    {
     "label": "Keep Rule",
     "value": 0.0,
     "of": 8,
     "unit": "count",
     "text": null,
     "sub": "baseline kept",
     "tone": "neutral"
    },
    {
     "label": "Beats Baseline",
     "value": 4.0,
     "of": 8,
     "unit": "count",
     "text": null,
     "sub": "pinball, both years",
     "tone": "neutral"
    },
    {
     "label": "Coverage Error",
     "value": 0.5617753847423361,
     "of": null,
     "unit": "pts",
     "text": null,
     "sub": "shipped, vol h42 · 2023",
     "tone": "neg"
    }
   ]
  },
  "headline": [
   {
    "label": "Keep Rule",
    "value": 0.0,
    "of": 8,
    "unit": "count",
    "text": null,
    "sub": "targets on LightGBM",
    "tone": "neutral"
   },
   {
    "label": "Vol vs Baseline",
    "value": -0.329531,
    "of": null,
    "unit": "pct",
    "text": null,
    "sub": "pinball, h21 · 2023",
    "tone": "pos"
   },
   {
    "label": "Return vs Baseline",
    "value": 0.160835,
    "of": null,
    "unit": "pct",
    "text": null,
    "sub": "pinball, h21 · 2023",
    "tone": "neg"
   },
   {
    "label": "Coverage Error",
    "value": 0.5617753847423361,
    "of": null,
    "unit": "pts",
    "text": null,
    "sub": "shipped, vol h42 · 2023",
    "tone": "neg"
   },
   {
    "label": "Rank IC",
    "value": 0.8755007827997655,
    "of": null,
    "unit": "ratio",
    "text": null,
    "sub": "vol h21 · 2023",
    "tone": "neutral"
   },
   {
    "label": "Holdout Reads",
    "value": 0.0,
    "of": null,
    "unit": "count",
    "text": null,
    "sub": "dev only",
    "tone": "neutral"
   }
  ],
  "spec": [
   {
    "label": "Targets",
    "value": "return · vol"
   },
   {
    "label": "Horizons",
    "value": "5 · 10 · 21 · 42 sessions"
   },
   {
    "label": "Quantiles",
    "value": "5 · 10 · 25 · 50 · 75 · 90 · 95"
   },
   {
    "label": "Universe",
    "value": "481 names · 459,990 rows"
   },
   {
    "label": "Train Window",
    "value": "2020-07-01 → 2023-12-31"
   },
   {
    "label": "Validation",
    "value": "walk-forward · test 2022, 2023 · purge labels inside dev"
   },
   {
    "label": "Baselines",
    "value": "historical quantiles (return) · HAR-IV (vol)"
   },
   {
    "label": "Calibration",
    "value": "conformal (CQR) per level, 2023-01-01 → 2023-12-31"
   },
   {
    "label": "Params",
    "value": "300 rounds · lr 0.05 · 31 leaves · min leaf 1000"
   },
   {
    "label": "Seed",
    "value": "20261017 · deterministic"
   },
   {
    "label": "Features",
    "value": "85 (Ticker Price 57 · Ticker Options 9 · Market 8 · Calendar 7 · Event 4)"
   },
   {
    "label": "Keep Rule",
    "value": "beats baseline (DM p < 0.05) and coverage error ≤ 3 pts, both years"
   },
   {
    "label": "Lineage",
    "value": "schema 45ba7c64e725 · data 19b0c6288a2d17ff · code a81d753de9"
   }
  ],
  "feature_groups": {
   "ticker_price": 57,
   "ticker_options": 9,
   "market": 8,
   "calendar": 7,
   "event": 4
  },
  "importance": [
   {
    "target": "Return h5",
    "kind": "gain",
    "rows": [
     [
      "spy_dd_252",
      0.0914
     ],
     [
      "sess_to_fomc",
      0.0778
     ],
     [
      "spy_dist_ma200",
      0.0776
     ],
     [
      "vix_proxy",
      0.0737
     ],
     [
      "spy_dist_ma50",
      0.0728
     ],
     [
      "spy_flip_age",
      0.0663
     ],
     [
      "spy_trend_z",
      0.0643
     ],
     [
      "vix_proxy_pct_252",
      0.0577
     ],
     [
      "sess_to_cpi",
      0.0475
     ],
     [
      "sess_to_nfp",
      0.0452
     ]
    ]
   },
   {
    "target": "Return h10",
    "kind": "gain",
    "rows": [
     [
      "spy_dd_252",
      0.0903
     ],
     [
      "spy_dist_ma200",
      0.0871
     ],
     [
      "sess_to_fomc",
      0.0766
     ],
     [
      "vix_proxy",
      0.0763
     ],
     [
      "spy_dist_ma50",
      0.075
     ],
     [
      "vix_proxy_pct_252",
      0.0557
     ],
     [
      "spy_flip_age",
      0.0477
     ],
     [
      "spy_trend_z",
      0.0461
     ],
     [
      "sess_to_nfp",
      0.0377
     ],
     [
      "corr_60",
      0.0354
     ]
    ]
   },
   {
    "target": "Return h21",
    "kind": "gain",
    "rows": [
     [
      "spy_dd_252",
      0.1006
     ],
     [
      "vix_proxy",
      0.0732
     ],
     [
      "spy_dist_ma200",
      0.0719
     ],
     [
      "vix_proxy_pct_252",
      0.0671
     ],
     [
      "spy_dist_ma50",
      0.0544
     ],
     [
      "sess_to_fomc",
      0.0523
     ],
     [
      "corr_60",
      0.05
     ],
     [
      "spy_trend_z",
      0.0411
     ],
     [
      "beta_60",
      0.0304
     ],
     [
      "spy_flip_age",
      0.0287
     ]
    ]
   },
   {
    "target": "Return h42",
    "kind": "gain",
    "rows": [
     [
      "spy_dd_252",
      0.0886
     ],
     [
      "vix_proxy_pct_252",
      0.063
     ],
     [
      "spy_dist_ma200",
      0.0617
     ],
     [
      "vix_proxy",
      0.0539
     ],
     [
      "corr_60",
      0.0532
     ],
     [
      "spy_dist_ma50",
      0.0512
     ],
     [
      "beta_60",
      0.0376
     ],
     [
      "hq_q05_h42",
      0.0305
     ],
     [
      "spy_trend_z",
      0.0272
     ],
     [
      "vol_pct_v2",
      0.0269
     ]
    ]
   },
   {
    "target": "Vol h5",
    "kind": "gain",
    "rows": [
     [
      "iv30_over_har",
      0.1528
     ],
     [
      "vix_proxy_pct_252",
      0.0788
     ],
     [
      "sess_to_fomc",
      0.0534
     ],
     [
      "iv_pct",
      0.0484
     ],
     [
      "spy_dd_252",
      0.0404
     ],
     [
      "spy_dist_ma200",
      0.0392
     ],
     [
      "vrp20",
      0.0374
     ],
     [
      "spy_flip_age",
      0.0341
     ],
     [
      "spy_dist_ma50",
      0.0305
     ],
     [
      "har_rv_h5",
      0.0304
     ]
    ]
   },
   {
    "target": "Vol h10",
    "kind": "gain",
    "rows": [
     [
      "iv30_over_har",
      0.2076
     ],
     [
      "vix_proxy_pct_252",
      0.0732
     ],
     [
      "iv_pct",
      0.0573
     ],
     [
      "spy_dist_ma200",
      0.0503
     ],
     [
      "vrp20",
      0.0409
     ],
     [
      "sess_to_fomc",
      0.0393
     ],
     [
      "spy_dd_252",
      0.0332
     ],
     [
      "spy_dist_ma50",
      0.0273
     ],
     [
      "vix_proxy",
      0.026
     ],
     [
      "spy_flip_age",
      0.0256
     ]
    ]
   },
   {
    "target": "Vol h21",
    "kind": "gain",
    "rows": [
     [
      "iv30_over_har",
      0.2531
     ],
     [
      "vix_proxy_pct_252",
      0.0798
     ],
     [
      "iv_pct",
      0.063
     ],
     [
      "spy_dist_ma200",
      0.0543
     ],
     [
      "vrp20",
      0.0426
     ],
     [
      "har_rv_h21",
      0.0294
     ],
     [
      "spy_dd_252",
      0.0293
     ],
     [
      "vix_proxy",
      0.0265
     ],
     [
      "spy_trend_z",
      0.0252
     ],
     [
      "sess_to_fomc",
      0.0227
     ]
    ]
   },
   {
    "target": "Vol h42",
    "kind": "gain",
    "rows": [
     [
      "iv30_over_har",
      0.1672
     ],
     [
      "vix_proxy_pct_252",
      0.0973
     ],
     [
      "spy_dist_ma200",
      0.0617
     ],
     [
      "har_rv_h42",
      0.0573
     ],
     [
      "iv_pct",
      0.0427
     ],
     [
      "vrp20",
      0.0396
     ],
     [
      "spy_dd_252",
      0.0387
     ],
     [
      "vix_proxy",
      0.0344
     ],
     [
      "hq_q05_h42",
      0.0303
     ],
     [
      "pk60",
      0.0217
     ]
    ]
   },
   {
    "target": "Return h21",
    "kind": "permutation",
    "rows": [
     [
      "iv30_over_har",
      0.002692
     ],
     [
      "vix_proxy",
      0.001885
     ],
     [
      "ret_60d",
      0.001064
     ],
     [
      "spy_dist_ma50",
      0.000896
     ],
     [
      "ret_252_21",
      0.000743
     ],
     [
      "hq_q05_h42",
      0.000518
     ],
     [
      "spy_dd_252",
      0.000204
     ],
     [
      "vol_pct_v2",
      0.000115
     ],
     [
      "vrp20",
      6.3e-05
     ],
     [
      "vix_proxy_pct_252",
      4.5e-05
     ]
    ]
   },
   {
    "target": "Vol h21",
    "kind": "permutation",
    "rows": [
     [
      "iv30_over_har",
      0.018247
     ],
     [
      "vol_pct_v2",
      0.000718
     ],
     [
      "hq_q05_h42",
      0.000709
     ],
     [
      "har_rv_h21",
      0.000706
     ],
     [
      "vrp20",
      0.000639
     ],
     [
      "sess_to_fomc",
      0.000619
     ],
     [
      "vix_proxy",
      0.000517
     ],
     [
      "rv20",
      0.000341
     ],
     [
      "iv_rank",
      0.000238
     ],
     [
      "spy_dd_252",
      0.000206
     ]
    ]
   }
  ],
  "regimes": [
   {
    "regime": "Spy Trend · Bear",
    "target": "Return h21",
    "year": "2022",
    "n": 24029,
    "rel": 0.3643,
    "cov_err": 0.324504,
    "ece": null,
    "er_ic": null
   },
   {
    "regime": "Spy Trend · Bull",
    "target": "Return h42",
    "year": "2022",
    "n": 11069,
    "rel": 0.344,
    "cov_err": 0.427026,
    "ece": null,
    "er_ic": null
   },
   {
    "regime": "Class · Etf",
    "target": "Return h21",
    "year": "2022",
    "n": 22294,
    "rel": 0.336,
    "cov_err": 0.315578,
    "ece": null,
    "er_ic": null
   },
   {
    "regime": "Spy Trend · Bear",
    "target": "Return h5",
    "year": "2022",
    "n": 24037,
    "rel": 0.3208,
    "cov_err": 0.297614,
    "ece": null,
    "er_ic": null
   },
   {
    "regime": "Earnings In H · No",
    "target": "Return h21",
    "year": "2022",
    "n": 72981,
    "rel": 0.2995,
    "cov_err": 0.336368,
    "ece": null,
    "er_ic": null
   }
  ],
  "loss": [
   {
    "group": "Return",
    "label": "h5",
    "year": "2022",
    "rel": 0.181161
   },
   {
    "group": "Return",
    "label": "h5",
    "year": "2023",
    "rel": 0.062234
   },
   {
    "group": "Return",
    "label": "h10",
    "year": "2022",
    "rel": 0.156368
   },
   {
    "group": "Return",
    "label": "h10",
    "year": "2023",
    "rel": 0.041502
   },
   {
    "group": "Return",
    "label": "h21",
    "year": "2022",
    "rel": 0.21657
   },
   {
    "group": "Return",
    "label": "h21",
    "year": "2023",
    "rel": 0.160835
   },
   {
    "group": "Return",
    "label": "h42",
    "year": "2022",
    "rel": 0.163039
   },
   {
    "group": "Return",
    "label": "h42",
    "year": "2023",
    "rel": 0.034627
   },
   {
    "group": "Vol",
    "label": "h5",
    "year": "2022",
    "rel": -0.066476
   },
   {
    "group": "Vol",
    "label": "h5",
    "year": "2023",
    "rel": -0.161114
   },
   {
    "group": "Vol",
    "label": "h10",
    "year": "2022",
    "rel": -0.117083
   },
   {
    "group": "Vol",
    "label": "h10",
    "year": "2023",
    "rel": -0.242237
   },
   {
    "group": "Vol",
    "label": "h21",
    "year": "2022",
    "rel": -0.071791
   },
   {
    "group": "Vol",
    "label": "h21",
    "year": "2023",
    "rel": -0.329531
   },
   {
    "group": "Vol",
    "label": "h42",
    "year": "2022",
    "rel": -0.089261
   },
   {
    "group": "Vol",
    "label": "h42",
    "year": "2023",
    "rel": -0.327036
   }
  ],
  "versions": [
   {
    "version": "m1-53833784f276",
    "status": "champion",
    "created": "2026-10-18T02:05:04.618595Z",
    "rule_passed": null,
    "source": "card",
    "train_s": 3259.00137495785,
    "note": null
   },
   {
    "version": "m1-c7acc7dfb7b7",
    "status": "retired",
    "created": "2026-10-18T01:09:44.975494Z",
    "rule_passed": null,
    "source": "card",
    "train_s": 3010.761999957962,
    "note": null
   }
  ],
  "cost": {
   "train_s": 3259.00137495785,
   "folds_s": 2245.9287999600638,
   "final_s": 694.3767770829145,
   "predict_s": 0.08377575012855232,
   "predict_rows": 469,
   "predict_all_s": 1.189544916851446,
   "size_bytes": 53212679,
   "boosters": 56,
   "peak_rss_mb": null
  },
  "live": null,
  "family_kind": "quantile",
  "rule": [
   {
    "target": "Return h5",
    "kind": "log_ret",
    "h": 5,
    "ships": "baseline",
    "years": [
     {
      "year": "2022",
      "pinball_rel": 0.181161,
      "dm_p": 0.9997307808852387,
      "cov_err": 0.227753,
      "beats": false,
      "cov_ok": false,
      "passed": false
     },
     {
      "year": "2023",
      "pinball_rel": 0.062234,
      "dm_p": 0.9999630907973526,
      "cov_err": 0.121093,
      "beats": false,
      "cov_ok": false,
      "passed": false
     }
    ]
   },
   {
    "target": "Return h10",
    "kind": "log_ret",
    "h": 10,
    "ships": "baseline",
    "years": [
     {
      "year": "2022",
      "pinball_rel": 0.156368,
      "dm_p": 0.9921554161892361,
      "cov_err": 0.256953,
      "beats": false,
      "cov_ok": false,
      "passed": false
     },
     {
      "year": "2023",
      "pinball_rel": 0.041502,
      "dm_p": 0.9995663378881641,
      "cov_err": 0.093437,
      "beats": false,
      "cov_ok": false,
      "passed": false
     }
    ]
   },
   {
    "target": "Return h21",
    "kind": "log_ret",
    "h": 21,
    "ships": "baseline",
    "years": [
     {
      "year": "2022",
      "pinball_rel": 0.21657,
      "dm_p": 0.9787665604165077,
      "cov_err": 0.312491,
      "beats": false,
      "cov_ok": false,
      "passed": false
     },
     {
      "year": "2023",
      "pinball_rel": 0.160835,
      "dm_p": 0.9999530280963982,
      "cov_err": 0.195695,
      "beats": false,
      "cov_ok": false,
      "passed": false
     }
    ]
   },
   {
    "target": "Return h42",
    "kind": "log_ret",
    "h": 42,
    "ships": "baseline",
    "years": [
     {
      "year": "2022",
      "pinball_rel": 0.163039,
      "dm_p": 0.9159737207019394,
      "cov_err": 0.324491,
      "beats": false,
      "cov_ok": false,
      "passed": false
     },
     {
      "year": "2023",
      "pinball_rel": 0.034627,
      "dm_p": 0.8985894491517202,
      "cov_err": 0.24211,
      "beats": false,
      "cov_ok": false,
      "passed": false
     }
    ]
   },
   {
    "target": "Vol h5",
    "kind": "rv",
    "h": 5,
    "ships": "baseline",
    "years": [
     {
      "year": "2022",
      "pinball_rel": -0.066476,
      "dm_p": 3.407132541370938e-05,
      "cov_err": 0.052781,
      "beats": true,
      "cov_ok": false,
      "passed": false
     },
     {
      "year": "2023",
      "pinball_rel": -0.161114,
      "dm_p": 6.085854227337105e-44,
      "cov_err": 0.084047,
      "beats": true,
      "cov_ok": false,
      "passed": false
     }
    ]
   },
   {
    "target": "Vol h10",
    "kind": "rv",
    "h": 10,
    "ships": "baseline",
    "years": [
     {
      "year": "2022",
      "pinball_rel": -0.117083,
      "dm_p": 7.041107287598009e-05,
      "cov_err": 0.115483,
      "beats": true,
      "cov_ok": false,
      "passed": false
     },
     {
      "year": "2023",
      "pinball_rel": -0.242237,
      "dm_p": 2.781127318299814e-30,
      "cov_err": 0.114417,
      "beats": true,
      "cov_ok": false,
      "passed": false
     }
    ]
   },
   {
    "target": "Vol h21",
    "kind": "rv",
    "h": 21,
    "ships": "baseline",
    "years": [
     {
      "year": "2022",
      "pinball_rel": -0.071791,
      "dm_p": 0.00015990517830876715,
      "cov_err": 0.239657,
      "beats": true,
      "cov_ok": false,
      "passed": false
     },
     {
      "year": "2023",
      "pinball_rel": -0.329531,
      "dm_p": 9.734703873848335e-18,
      "cov_err": 0.238303,
      "beats": true,
      "cov_ok": false,
      "passed": false
     }
    ]
   },
   {
    "target": "Vol h42",
    "kind": "rv",
    "h": 42,
    "ships": "baseline",
    "years": [
     {
      "year": "2022",
      "pinball_rel": -0.089261,
      "dm_p": 3.461406972951479e-05,
      "cov_err": 0.353675,
      "beats": true,
      "cov_ok": false,
      "passed": false
     },
     {
      "year": "2023",
      "pinball_rel": -0.327036,
      "dm_p": 5.1113719652288896e-14,
      "cov_err": 0.342441,
      "beats": true,
      "cov_ok": false,
      "passed": false
     }
    ]
   }
  ],
  "coverage": [
   {
    "target": "Return h5",
    "year": "2022",
    "model": "lgbm",
    "points": [
     [
      0.05,
      0.1687
     ],
     [
      0.1,
      0.2833
     ],
     [
      0.25,
      0.4778
     ],
     [
      0.5,
      0.6916
     ],
     [
      0.75,
      0.8855
     ],
     [
      0.9,
      0.968
     ],
     [
      0.95,
      0.9851
     ]
    ]
   },
   {
    "target": "Return h5",
    "year": "2022",
    "model": "baseline",
    "points": [
     [
      0.05,
      0.0751
     ],
     [
      0.1,
      0.1292
     ],
     [
      0.25,
      0.2773
     ],
     [
      0.5,
      0.4984
     ],
     [
      0.75,
      0.7189
     ],
     [
      0.9,
      0.8664
     ],
     [
      0.95,
      0.926
     ]
    ]
   },
   {
    "target": "Return h5",
    "year": "2023",
    "model": "lgbm",
    "points": [
     [
      0.05,
      0.0098
     ],
     [
      0.1,
      0.0254
     ],
     [
      0.25,
      0.1289
     ],
     [
      0.5,
      0.3951
     ],
     [
      0.75,
      0.7275
     ],
     [
      0.9,
      0.8923
     ],
     [
      0.95,
      0.9428
     ]
    ]
   },
   {
    "target": "Return h5",
    "year": "2023",
    "model": "baseline",
    "points": [
     [
      0.05,
      0.022
     ],
     [
      0.1,
      0.0517
     ],
     [
      0.25,
      0.1741
     ],
     [
      0.5,
      0.4576
     ],
     [
      0.75,
      0.7556
     ],
     [
      0.9,
      0.9129
     ],
     [
      0.95,
      0.9583
     ]
    ]
   },
   {
    "target": "Return h10",
    "year": "2022",
    "model": "lgbm",
    "points": [
     [
      0.05,
      0.145
     ],
     [
      0.1,
      0.2583
     ],
     [
      0.25,
      0.5015
     ],
     [
      0.5,
      0.757
     ],
     [
      0.75,
      0.9095
     ],
     [
      0.9,
      0.9754
     ],
     [
      0.95,
      0.9893
     ]
    ]
   },
   {
    "target": "Return h10",
    "year": "2022",
    "model": "baseline",
    "points": [
     [
      0.05,
      0.0786
     ],
     [
      0.1,
      0.134
     ],
     [
      0.25,
      0.2741
     ],
     [
      0.5,
      0.4933
     ],
     [
      0.75,
      0.7119
     ],
     [
      0.9,
      0.8578
     ],
     [
      0.95,
      0.919
     ]
    ]
   },
   {
    "target": "Return h10",
    "year": "2023",
    "model": "lgbm",
    "points": [
     [
      0.05,
      0.0055
     ],
     [
      0.1,
      0.0186
     ],
     [
      0.25,
      0.1566
     ],
     [
      0.5,
      0.4604
     ],
     [
      0.75,
      0.7453
     ],
     [
      0.9,
      0.9054
     ],
     [
      0.95,
      0.9581
     ]
    ]
   },
   {
    "target": "Return h10",
    "year": "2023",
    "model": "baseline",
    "points": [
     [
      0.05,
      0.02
     ],
     [
      0.1,
      0.0467
     ],
     [
      0.25,
      0.1657
     ],
     [
      0.5,
      0.4429
     ],
     [
      0.75,
      0.7405
     ],
     [
      0.9,
      0.9049
     ],
     [
      0.95,
      0.9544
     ]
    ]
   },
   {
    "target": "Return h21",
    "year": "2022",
    "model": "lgbm",
    "points": [
     [
      0.05,
      0.1505
     ],
     [
      0.1,
      0.2621
     ],
     [
      0.25,
      0.5224
     ],
     [
      0.5,
      0.8125
     ],
     [
      0.75,
      0.9338
     ],
     [
      0.9,
      0.982
     ],
     [
      0.95,
      0.9911
     ]
    ]
   },
   {
    "target": "Return h21",
    "year": "2022",
    "model": "baseline",
    "points": [
     [
      0.05,
      0.0759
     ],
     [
      0.1,
      0.1276
     ],
     [
      0.25,
      0.2629
     ],
     [
      0.5,
      0.4846
     ],
     [
      0.75,
      0.7013
     ],
     [
      0.9,
      0.8443
     ],
     [
      0.95,
      0.9027
     ]
    ]
   },
   {
    "target": "Return h21",
    "year": "2023",
    "model": "lgbm",
    "points": [
     [
      0.05,
      0.0032
     ],
     [
      0.1,
      0.01
     ],
     [
      0.25,
      0.0732
     ],
     [
      0.5,
      0.3043
     ],
     [
      0.75,
      0.6328
     ],
     [
      0.9,
      0.8546
     ],
     [
      0.95,
      0.9302
     ]
    ]
   },
   {
    "target": "Return h21",
    "year": "2023",
    "model": "baseline",
    "points": [
     [
      0.05,
      0.0112
     ],
     [
      0.1,
      0.0283
     ],
     [
      0.25,
      0.1327
     ],
     [
      0.5,
      0.4083
     ],
     [
      0.75,
      0.7356
     ],
     [
      0.9,
      0.9054
     ],
     [
      0.95,
      0.9569
     ]
    ]
   },
   {
    "target": "Return h42",
    "year": "2022",
    "model": "lgbm",
    "points": [
     [
      0.05,
      0.2206
     ],
     [
      0.1,
      0.3372
     ],
     [
      0.25,
      0.5682
     ],
     [
      0.5,
      0.8245
     ],
     [
      0.75,
      0.9327
     ],
     [
      0.9,
      0.9799
     ],
     [
      0.95,
      0.9924
     ]
    ]
   },
   {
    "target": "Return h42",
    "year": "2022",
    "model": "baseline",
    "points": [
     [
      0.05,
      0.0873
     ],
     [
      0.1,
      0.1502
     ],
     [
      0.25,
      0.2845
     ],
     [
      0.5,
      0.4921
     ],
     [
      0.75,
      0.7084
     ],
     [
      0.9,
      0.8544
     ],
     [
      0.95,
      0.9121
     ]
    ]
   },
   {
    "target": "Return h42",
    "year": "2023",
    "model": "lgbm",
    "points": [
     [
      0.05,
      0.0018
     ],
     [
      0.1,
      0.0072
     ],
     [
      0.25,
      0.0531
     ],
     [
      0.5,
      0.2579
     ],
     [
      0.75,
      0.6394
     ],
     [
      0.9,
      0.8721
     ],
     [
      0.95,
      0.9346
     ]
    ]
   },
   {
    "target": "Return h42",
    "year": "2023",
    "model": "baseline",
    "points": [
     [
      0.05,
      0.0086
     ],
     [
      0.1,
      0.0204
     ],
     [
      0.25,
      0.0964
     ],
     [
      0.5,
      0.348
     ],
     [
      0.75,
      0.6683
     ],
     [
      0.9,
      0.8682
     ],
     [
      0.95,
      0.9376
     ]
    ]
   },
   {
    "target": "Vol h5",
    "year": "2022",
    "model": "lgbm",
    "points": [
     [
      0.05,
      0.0357
     ],
     [
      0.1,
      0.0729
     ],
     [
      0.25,
      0.2009
     ],
     [
      0.5,
      0.4472
     ],
     [
      0.75,
      0.7106
     ],
     [
      0.9,
      0.8733
     ],
     [
      0.95,
      0.9282
     ]
    ]
   },
   {
    "target": "Vol h5",
    "year": "2022",
    "model": "baseline",
    "points": [
     [
      0.05,
      0.0182
     ],
     [
      0.1,
      0.0411
     ],
     [
      0.25,
      0.1303
     ],
     [
      0.5,
      0.332
     ],
     [
      0.75,
      0.6209
     ],
     [
      0.9,
      0.8396
     ],
     [
      0.95,
      0.9141
     ]
    ]
   },
   {
    "target": "Vol h5",
    "year": "2023",
    "model": "lgbm",
    "points": [
     [
      0.05,
      0.0557
     ],
     [
      0.1,
      0.115
     ],
     [
      0.25,
      0.3014
     ],
     [
      0.5,
      0.584
     ],
     [
      0.75,
      0.818
     ],
     [
      0.9,
      0.9403
     ],
     [
      0.95,
      0.9704
     ]
    ]
   },
   {
    "target": "Vol h5",
    "year": "2023",
    "model": "baseline",
    "points": [
     [
      0.05,
      0.1183
     ],
     [
      0.1,
      0.2109
     ],
     [
      0.25,
      0.4381
     ],
     [
      0.5,
      0.7034
     ],
     [
      0.75,
      0.8801
     ],
     [
      0.9,
      0.9546
     ],
     [
      0.95,
      0.9741
     ]
    ]
   },
   {
    "target": "Vol h10",
    "year": "2022",
    "model": "lgbm",
    "points": [
     [
      0.05,
      0.0117
     ],
     [
      0.1,
      0.0302
     ],
     [
      0.25,
      0.1345
     ],
     [
      0.5,
      0.4092
     ],
     [
      0.75,
      0.6772
     ],
     [
      0.9,
      0.8819
     ],
     [
      0.95,
      0.9447
     ]
    ]
   },
   {
    "target": "Vol h10",
    "year": "2022",
    "model": "baseline",
    "points": [
     [
      0.05,
      0.0111
     ],
     [
      0.1,
      0.0258
     ],
     [
      0.25,
      0.0885
     ],
     [
      0.5,
      0.26
     ],
     [
      0.75,
      0.5529
     ],
     [
      0.9,
      0.7934
     ],
     [
      0.95,
      0.9006
     ]
    ]
   },
   {
    "target": "Vol h10",
    "year": "2023",
    "model": "lgbm",
    "points": [
     [
      0.05,
      0.0378
     ],
     [
      0.1,
      0.0911
     ],
     [
      0.25,
      0.3083
     ],
     [
      0.5,
      0.6144
     ],
     [
      0.75,
      0.8568
     ],
     [
      0.9,
      0.9534
     ],
     [
      0.95,
      0.9775
     ]
    ]
   },
   {
    "target": "Vol h10",
    "year": "2023",
    "model": "baseline",
    "points": [
     [
      0.05,
      0.1496
     ],
     [
      0.1,
      0.2729
     ],
     [
      0.25,
      0.5187
     ],
     [
      0.5,
      0.7614
     ],
     [
      0.75,
      0.9086
     ],
     [
      0.9,
      0.9647
     ],
     [
      0.95,
      0.9811
     ]
    ]
   },
   {
    "target": "Vol h21",
    "year": "2022",
    "model": "lgbm",
    "points": [
     [
      0.05,
      0.0031
     ],
     [
      0.1,
      0.0119
     ],
     [
      0.25,
      0.069
     ],
     [
      0.5,
      0.2645
     ],
     [
      0.75,
      0.5103
     ],
     [
      0.9,
      0.8176
     ],
     [
      0.95,
      0.9097
     ]
    ]
   },
   {
    "target": "Vol h21",
    "year": "2022",
    "model": "baseline",
    "points": [
     [
      0.05,
      0.0077
     ],
     [
      0.1,
      0.0168
     ],
     [
      0.25,
      0.0551
     ],
     [
      0.5,
      0.2012
     ],
     [
      0.75,
      0.4856
     ],
     [
      0.9,
      0.7651
     ],
     [
      0.95,
      0.8932
     ]
    ]
   },
   {
    "target": "Vol h21",
    "year": "2023",
    "model": "lgbm",
    "points": [
     [
      0.05,
      0.0647
     ],
     [
      0.1,
      0.1595
     ],
     [
      0.25,
      0.3955
     ],
     [
      0.5,
      0.7383
     ],
     [
      0.75,
      0.9229
     ],
     [
      0.9,
      0.9766
     ],
     [
      0.95,
      0.9897
     ]
    ]
   },
   {
    "target": "Vol h21",
    "year": "2023",
    "model": "baseline",
    "points": [
     [
      0.05,
      0.2222
     ],
     [
      0.1,
      0.3819
     ],
     [
      0.25,
      0.6593
     ],
     [
      0.5,
      0.8623
     ],
     [
      0.75,
      0.9515
     ],
     [
      0.9,
      0.9829
     ],
     [
      0.95,
      0.9908
     ]
    ]
   },
   {
    "target": "Vol h42",
    "year": "2022",
    "model": "lgbm",
    "points": [
     [
      0.05,
      0.002
     ],
     [
      0.1,
      0.0056
     ],
     [
      0.25,
      0.0448
     ],
     [
      0.5,
      0.1857
     ],
     [
      0.75,
      0.3963
     ],
     [
      0.9,
      0.6984
     ],
     [
      0.95,
      0.8474
     ]
    ]
   },
   {
    "target": "Vol h42",
    "year": "2022",
    "model": "baseline",
    "points": [
     [
      0.05,
      0.0036
     ],
     [
      0.1,
      0.0057
     ],
     [
      0.25,
      0.0203
     ],
     [
      0.5,
      0.1304
     ],
     [
      0.75,
      0.3763
     ],
     [
      0.9,
      0.6508
     ],
     [
      0.95,
      0.8253
     ]
    ]
   },
   {
    "target": "Vol h42",
    "year": "2023",
    "model": "lgbm",
    "points": [
     [
      0.05,
      0.1662
     ],
     [
      0.1,
      0.2399
     ],
     [
      0.25,
      0.5085
     ],
     [
      0.5,
      0.8424
     ],
     [
      0.75,
      0.9751
     ],
     [
      0.9,
      0.9937
     ],
     [
      0.95,
      0.9971
     ]
    ]
   },
   {
    "target": "Vol h42",
    "year": "2023",
    "model": "baseline",
    "points": [
     [
      0.05,
      0.3728
     ],
     [
      0.1,
      0.5747
     ],
     [
      0.25,
      0.8118
     ],
     [
      0.5,
      0.9367
     ],
     [
      0.75,
      0.9796
     ],
     [
      0.9,
      0.9911
     ],
     [
      0.95,
      0.9944
     ]
    ]
   }
  ],
  "rolling": [
   {
    "year": "2022",
    "label": "h5",
    "annual": 0.052781,
    "rolling": 0.010371
   },
   {
    "year": "2023",
    "label": "h5",
    "annual": 0.084047,
    "rolling": 0.024826
   },
   {
    "year": "2022",
    "label": "h10",
    "annual": 0.115483,
    "rolling": 0.035447
   },
   {
    "year": "2023",
    "label": "h10",
    "annual": 0.114417,
    "rolling": 0.023882
   },
   {
    "year": "2022",
    "label": "h21",
    "annual": 0.239657,
    "rolling": 0.067947
   },
   {
    "year": "2023",
    "label": "h21",
    "annual": 0.238303,
    "rolling": 0.085431
   },
   {
    "year": "2022",
    "label": "h42",
    "annual": 0.353675,
    "rolling": 0.149965
   },
   {
    "year": "2023",
    "label": "h42",
    "annual": 0.342441,
    "rolling": 0.101353
   }
  ]
 },
 "scope": {
  "arm_id": null
 },
 "shared_from_control": false
};

export const DEBIT_DETAIL = {
 "detail": {
  "as_of": "2026-10-28T09:00:00Z",
  "model": {
   "family": "debit",
   "label": "M2 · Candidate Outcome (Debit)",
   "family_kind": "outcome",
   "purpose": "The same targets for debit candidates.",
   "version": "m2-04e58916e118",
   "status": "candidate",
   "created": "2026-10-26T16:51:52.808075-04:00",
   "source": "card",
   "rule": "m2_promote",
   "rule_passed": false,
   "holdout_reads": 0,
   "stats": [
    {
     "label": "Promote Rule",
     "value": 0.0,
     "of": 2,
     "unit": "count",
     "text": null,
     "sub": "test years passed",
     "tone": "neutral"
    },
    {
     "label": "Log-Loss vs Logistic",
     "value": 0.066051,
     "of": null,
     "unit": "pct",
     "text": null,
     "sub": "win, 2023",
     "tone": "neg"
    },
    {
     "label": "E[R] IC",
     "value": 0.14606757784487695,
     "of": null,
     "unit": "ratio",
     "text": null,
     "sub": "per day, 2023",
     "tone": "pos"
    }
   ]
  },
  "headline": [
   {
    "label": "Promote Rule",
    "value": 0.0,
    "of": 2,
    "unit": "count",
    "text": null,
    "sub": "test years passed",
    "tone": "neutral"
   },
   {
    "label": "Log-Loss vs Logistic",
    "value": 0.066051,
    "of": null,
    "unit": "pct",
    "text": null,
    "sub": "win, 2023",
    "tone": "neg"
   },
   {
    "label": "E[R] IC",
    "value": 0.14606757784487695,
    "of": null,
    "unit": "ratio",
    "text": null,
    "sub": "per day, 2023",
    "tone": "pos"
   },
   {
    "label": "Win ECE",
    "value": 0.10629822596319306,
    "of": null,
    "unit": "pts",
    "text": null,
    "sub": "calibrated, 2023",
    "tone": "neutral"
   },
   {
    "label": "Replay vs Incumbent",
    "value": -5264.57,
    "of": null,
    "unit": "usd",
    "text": null,
    "sub": "net, ranked by E[R], 2023",
    "tone": "neg"
   },
   {
    "label": "Holdout Reads",
    "value": 0.0,
    "of": null,
    "unit": "count",
    "text": null,
    "sub": "dev only",
    "tone": "neutral"
   }
  ],
  "spec": [
   {
    "label": "Targets",
    "value": "days · q10 · q50 · q90 · r · r_loss · r_win · stop · win"
   },
   {
    "label": "Profile",
    "value": "Cash Debit"
   },
   {
    "label": "Rows",
    "value": "602,625 train rows of 10,781,498 ledger rows · cap 600/day"
   },
   {
    "label": "Train Window",
    "value": "2020-01-01 → 2023-12-31"
   },
   {
    "label": "Validation",
    "value": "walk-forward · test 2022, 2023"
   },
   {
    "label": "Baselines",
    "value": "logistic · managed PoP"
   },
   {
    "label": "Calibration",
    "value": "isotonic, out of fold"
   },
   {
    "label": "Params",
    "value": "lr 0.03 · 6 grid points · 50 configs tried"
   },
   {
    "label": "Label Slippage",
    "value": "x025"
   },
   {
    "label": "Features",
    "value": "122 (Ticker Price 57 · Candidate Ledger 36 · Ticker Options 9 · Market 8 · Calendar 7 · Event 4 · Candidate 1)"
   },
   {
    "label": "Promote Rule",
    "value": "beats logistic and managed PoP on log-loss, per-day E[R] IC > 0 (NW t > 2), replay beats the incumbent on net and max DD, both years"
   },
   {
    "label": "Lineage",
    "value": "schema 45ba7c64e725 · data 10d5ccd68a65b71f · code 5af30f4c8b"
   }
  ],
  "feature_groups": {
   "ticker_price": 57,
   "candidate_ledger": 36,
   "ticker_options": 9,
   "market": 8,
   "calendar": 7,
   "event": 4,
   "candidate": 1
  },
  "importance": [
   {
    "target": "win",
    "kind": "gain",
    "rows": [
     [
      "managed_pop",
      0.2604
     ],
     [
      "cat_sleeve",
      0.2256
     ],
     [
      "vix_proxy",
      0.1082
     ],
     [
      "log_max_loss",
      0.0813
     ],
     [
      "sess_to_fomc",
      0.0548
     ],
     [
      "spy_dd_252",
      0.0499
     ],
     [
      "spy_dist_ma200",
      0.0433
     ],
     [
      "vix_proxy_pct_252",
      0.0411
     ],
     [
      "cost_per_risk",
      0.0342
     ],
     [
      "spy_flip_age",
      0.0263
     ]
    ]
   },
   {
    "target": "stop",
    "kind": "gain",
    "rows": [
     [
      "log_max_loss",
      0.1802
     ],
     [
      "opt_volume",
      0.1584
     ],
     [
      "dte",
      0.1217
     ],
     [
      "cand_spread_over_premium",
      0.1083
     ],
     [
      "ctx_liquidity",
      0.0639
     ],
     [
      "cost_per_risk",
      0.0524
     ],
     [
      "ctx_entry_spread_ok",
      0.0515
     ],
     [
      "cat_sleeve",
      0.0431
     ],
     [
      "log_leg_oi_min",
      0.0373
     ],
     [
      "atm_spread_pct",
      0.0365
     ]
    ]
   },
   {
    "target": "days",
    "kind": "gain",
    "rows": [
     [
      "dte",
      0.3625
     ],
     [
      "expected_days_held",
      0.1122
     ],
     [
      "atm_spread_pct",
      0.1121
     ],
     [
      "cat_sleeve",
      0.1092
     ],
     [
      "ctx_entry_spread_ok",
      0.0651
     ],
     [
      "opt_volume",
      0.0453
     ],
     [
      "log_max_loss",
      0.0428
     ],
     [
      "log_leg_volume_min",
      0.0187
     ],
     [
      "cand_spread_over_premium",
      0.0165
     ],
     [
      "gain_per_risk",
      0.0111
     ]
    ]
   },
   {
    "target": "q10",
    "kind": "gain",
    "rows": [
     [
      "ctx_entry_spread_ok",
      0.2188
     ],
     [
      "log_max_loss",
      0.1588
     ],
     [
      "opt_volume",
      0.1443
     ],
     [
      "cand_spread_over_premium",
      0.1374
     ],
     [
      "rorc_day",
      0.0835
     ],
     [
      "cost_per_risk",
      0.0415
     ],
     [
      "atm_spread_pct",
      0.041
     ],
     [
      "ctx_liquidity",
      0.0377
     ],
     [
      "managed_ev_per_risk",
      0.0232
     ],
     [
      "dte",
      0.0222
     ]
    ]
   },
   {
    "target": "q50",
    "kind": "gain",
    "rows": [
     [
      "dte",
      0.3709
     ],
     [
      "log_max_loss",
      0.1498
     ],
     [
      "managed_ev_per_risk",
      0.1044
     ],
     [
      "ev_proxy_per_risk",
      0.0533
     ],
     [
      "vix_proxy",
      0.0512
     ],
     [
      "cat_sleeve",
      0.0503
     ],
     [
      "rorc_day",
      0.0498
     ],
     [
      "sess_to_fomc",
      0.0228
     ],
     [
      "atm_spread_pct",
      0.0196
     ],
     [
      "be_sigma",
      0.0196
     ]
    ]
   },
   {
    "target": "q90",
    "kind": "gain",
    "rows": [
     [
      "gain_per_risk",
      0.1674
     ],
     [
      "dte",
      0.084
     ],
     [
      "log_max_loss",
      0.0656
     ],
     [
      "anchor_delta",
      0.0545
     ],
     [
      "opt_volume",
      0.0536
     ],
     [
      "sess_to_fomc",
      0.0416
     ],
     [
      "atm_spread_pct",
      0.0387
     ],
     [
      "ctx_entry_spread_ok",
      0.0372
     ],
     [
      "ev_proxy_per_risk",
      0.036
     ],
     [
      "static_pop",
      0.0354
     ]
    ]
   },
   {
    "target": "r",
    "kind": "gain",
    "rows": [
     [
      "rorc_day",
      0.422
     ],
     [
      "cand_spread_over_premium",
      0.0871
     ],
     [
      "cost_per_risk",
      0.0762
     ],
     [
      "dte",
      0.0739
     ],
     [
      "managed_ev_per_risk",
      0.0598
     ],
     [
      "log_max_loss",
      0.0374
     ],
     [
      "sess_to_fomc",
      0.0369
     ],
     [
      "vix_proxy",
      0.0358
     ],
     [
      "cat_sleeve",
      0.03
     ],
     [
      "spy_dd_252",
      0.0254
     ]
    ]
   },
   {
    "target": "r_loss",
    "kind": "gain",
    "rows": [
     [
      "dte",
      0.1509
     ],
     [
      "ctx_entry_spread_ok",
      0.1032
     ],
     [
      "anchor_delta",
      0.0934
     ],
     [
      "opt_volume",
      0.086
     ],
     [
      "cand_spread_over_premium",
      0.0554
     ],
     [
      "atm_spread_pct",
      0.0523
     ],
     [
      "log_max_loss",
      0.037
     ],
     [
      "cost_per_risk",
      0.0331
     ],
     [
      "be_sigma",
      0.0325
     ],
     [
      "cat_sleeve",
      0.0298
     ]
    ]
   },
   {
    "target": "r_win",
    "kind": "gain",
    "rows": [
     [
      "gain_per_risk",
      0.1496
     ],
     [
      "atm_spread_pct",
      0.0752
     ],
     [
      "opt_volume",
      0.0627
     ],
     [
      "dte",
      0.0586
     ],
     [
      "anchor_delta",
      0.0533
     ],
     [
      "sess_to_fomc",
      0.0396
     ],
     [
      "expected_days_held",
      0.0353
     ],
     [
      "cand_spread_over_premium",
      0.0278
     ],
     [
      "log_max_loss",
      0.0278
     ],
     [
      "static_pop",
      0.0258
     ]
    ]
   },
   {
    "target": "win",
    "kind": "permutation",
    "rows": [
     [
      "cat_kind",
      0.035111
     ],
     [
      "log_max_loss",
      0.002962
     ],
     [
      "cat_sleeve",
      0.002954
     ],
     [
      "cost_per_risk",
      0.001373
     ],
     [
      "managed_pop",
      0.000533
     ],
     [
      "rorc_day",
      0.000456
     ],
     [
      "cand_spread_over_premium",
      0.000362
     ],
     [
      "managed_ev_per_risk",
      0.000338
     ],
     [
      "spy_dd_252",
      0.000218
     ],
     [
      "vix_proxy_pct_252",
      1.8e-05
     ]
    ]
   }
  ],
  "regimes": [
   {
    "regime": "Trend · Bear",
    "target": "Win",
    "year": "2023",
    "n": 18329,
    "rel": null,
    "cov_err": null,
    "ece": 0.1314,
    "er_ic": 0.1257
   },
   {
    "regime": "Trend · Bear",
    "target": "Win",
    "year": "2022",
    "n": 25498,
    "rel": null,
    "cov_err": null,
    "ece": 0.1313,
    "er_ic": 0.1384
   },
   {
    "regime": "Trend · Sideways",
    "target": "Win",
    "year": "2022",
    "n": 105686,
    "rel": null,
    "cov_err": null,
    "ece": 0.1293,
    "er_ic": 0.0779
   },
   {
    "regime": "Vol · Low",
    "target": "Win",
    "year": "2022",
    "n": 24797,
    "rel": null,
    "cov_err": null,
    "ece": 0.1286,
    "er_ic": 0.0636
   },
   {
    "regime": "Vol · High",
    "target": "Win",
    "year": "2022",
    "n": 83017,
    "rel": null,
    "cov_err": null,
    "ece": 0.1238,
    "er_ic": 0.1032
   }
  ],
  "loss": [
   {
    "group": "All",
    "label": "Logistic",
    "year": "2022",
    "rel": 0.034432
   },
   {
    "group": "All",
    "label": "Managed PoP",
    "year": "2022",
    "rel": 0.039384
   },
   {
    "group": "Stocks",
    "label": "Logistic",
    "year": "2022",
    "rel": 0.030798
   },
   {
    "group": "Stocks",
    "label": "Managed PoP",
    "year": "2022",
    "rel": 0.040532
   },
   {
    "group": "ETFs",
    "label": "Logistic",
    "year": "2022",
    "rel": 0.050848
   },
   {
    "group": "ETFs",
    "label": "Managed PoP",
    "year": "2022",
    "rel": 0.034327
   },
   {
    "group": "All",
    "label": "Logistic",
    "year": "2023",
    "rel": 0.066051
   },
   {
    "group": "All",
    "label": "Managed PoP",
    "year": "2023",
    "rel": 0.022609
   },
   {
    "group": "Stocks",
    "label": "Logistic",
    "year": "2023",
    "rel": 0.063875
   },
   {
    "group": "Stocks",
    "label": "Managed PoP",
    "year": "2023",
    "rel": 0.018822
   },
   {
    "group": "ETFs",
    "label": "Logistic",
    "year": "2023",
    "rel": 0.076277
   },
   {
    "group": "ETFs",
    "label": "Managed PoP",
    "year": "2023",
    "rel": 0.040578
   }
  ],
  "versions": [
   {
    "version": "m2-04e58916e118",
    "status": "candidate",
    "created": "2026-10-26T16:51:52.808075-04:00",
    "rule_passed": false,
    "source": "card",
    "train_s": 233.5,
    "note": null
   }
  ],
  "cost": {
   "train_s": 233.5,
   "folds_s": 141.3,
   "final_s": 31.3,
   "predict_s": 0.031,
   "predict_rows": 256,
   "predict_all_s": null,
   "size_bytes": null,
   "boosters": null,
   "peak_rss_mb": 8259.0
  },
  "live": null,
  "family_kind": "outcome",
  "rule": [
   {
    "year": "2022",
    "ll_model": 0.735272,
    "ll_logistic": 0.710798,
    "ll_managed_pop": 0.707411,
    "beats_logistic": false,
    "beats_managed_pop": false,
    "er_ic": 0.08643,
    "er_ic_t": 1.773,
    "ic_pass": false,
    "replay_pass": false,
    "net_incumbent": -19803.24,
    "net_model": -22857.04,
    "dd_incumbent": -85.46,
    "dd_model": -93.58,
    "passed": false
   },
   {
    "year": "2023",
    "ll_model": 0.714833,
    "ll_logistic": 0.670543,
    "ll_managed_pop": 0.699028,
    "beats_logistic": false,
    "beats_managed_pop": false,
    "er_ic": 0.146068,
    "er_ic_t": 14.468,
    "ic_pass": true,
    "replay_pass": false,
    "net_incumbent": -1050.37,
    "net_model": -6314.94,
    "dd_incumbent": -59.89,
    "dd_model": -66.31,
    "passed": false
   }
  ],
  "reliability": [
   {
    "year": "2022",
    "target": "win",
    "bin": "0.0-0.1",
    "n": 3776,
    "mean_p": 0.0655,
    "observed": 0.2302
   },
   {
    "year": "2022",
    "target": "win",
    "bin": "0.1-0.2",
    "n": 17600,
    "mean_p": 0.1561,
    "observed": 0.3812
   },
   {
    "year": "2022",
    "target": "win",
    "bin": "0.2-0.3",
    "n": 30346,
    "mean_p": 0.2549,
    "observed": 0.4643
   },
   {
    "year": "2022",
    "target": "win",
    "bin": "0.3-0.4",
    "n": 27008,
    "mean_p": 0.3476,
    "observed": 0.4546
   },
   {
    "year": "2022",
    "target": "win",
    "bin": "0.4-0.5",
    "n": 29084,
    "mean_p": 0.4427,
    "observed": 0.4387
   },
   {
    "year": "2022",
    "target": "win",
    "bin": "0.5-0.6",
    "n": 22161,
    "mean_p": 0.5523,
    "observed": 0.462
   },
   {
    "year": "2022",
    "target": "win",
    "bin": "0.6-0.7",
    "n": 18689,
    "mean_p": 0.6293,
    "observed": 0.5257
   },
   {
    "year": "2022",
    "target": "win",
    "bin": "0.7-0.8",
    "n": 1527,
    "mean_p": 0.7312,
    "observed": 0.5043
   },
   {
    "year": "2022",
    "target": "win",
    "bin": "0.8-0.9",
    "n": 289,
    "mean_p": 0.8479,
    "observed": 0.4907
   },
   {
    "year": "2022",
    "target": "win",
    "bin": "0.9-1.0",
    "n": 121,
    "mean_p": 0.9285,
    "observed": 0.5899
   },
   {
    "year": "2022",
    "target": "stop",
    "bin": "0.0-0.1",
    "n": 4675,
    "mean_p": 0.0561,
    "observed": 0.0946
   },
   {
    "year": "2022",
    "target": "stop",
    "bin": "0.1-0.2",
    "n": 6225,
    "mean_p": 0.1577,
    "observed": 0.173
   },
   {
    "year": "2022",
    "target": "stop",
    "bin": "0.2-0.3",
    "n": 14472,
    "mean_p": 0.2598,
    "observed": 0.2527
   },
   {
    "year": "2022",
    "target": "stop",
    "bin": "0.3-0.4",
    "n": 35366,
    "mean_p": 0.3556,
    "observed": 0.3394
   },
   {
    "year": "2022",
    "target": "stop",
    "bin": "0.4-0.5",
    "n": 48251,
    "mean_p": 0.4556,
    "observed": 0.4216
   },
   {
    "year": "2022",
    "target": "stop",
    "bin": "0.5-0.6",
    "n": 30660,
    "mean_p": 0.5327,
    "observed": 0.5028
   },
   {
    "year": "2022",
    "target": "stop",
    "bin": "0.6-0.7",
    "n": 9114,
    "mean_p": 0.6431,
    "observed": 0.4846
   },
   {
    "year": "2022",
    "target": "stop",
    "bin": "0.7-0.8",
    "n": 1819,
    "mean_p": 0.7279,
    "observed": 0.5244
   },
   {
    "year": "2022",
    "target": "stop",
    "bin": "0.8-0.9",
    "n": 14,
    "mean_p": 0.8463,
    "observed": 0.6285
   },
   {
    "year": "2022",
    "target": "stop",
    "bin": "0.9-1.0",
    "n": 5,
    "mean_p": 0.9759,
    "observed": 0.7712
   },
   {
    "year": "2023",
    "target": "win",
    "bin": "0.4-0.5",
    "n": 36115,
    "mean_p": 0.4859,
    "observed": 0.3599
   },
   {
    "year": "2023",
    "target": "win",
    "bin": "0.5-0.6",
    "n": 79911,
    "mean_p": 0.5308,
    "observed": 0.4976
   },
   {
    "year": "2023",
    "target": "win",
    "bin": "0.6-0.7",
    "n": 33029,
    "mean_p": 0.6513,
    "observed": 0.4239
   },
   {
    "year": "2023",
    "target": "stop",
    "bin": "0.0-0.1",
    "n": 3503,
    "mean_p": 0.0649,
    "observed": 0.1309
   },
   {
    "year": "2023",
    "target": "stop",
    "bin": "0.1-0.2",
    "n": 13698,
    "mean_p": 0.1704,
    "observed": 0.2934
   },
   {
    "year": "2023",
    "target": "stop",
    "bin": "0.2-0.3",
    "n": 24536,
    "mean_p": 0.2624,
    "observed": 0.3935
   },
   {
    "year": "2023",
    "target": "stop",
    "bin": "0.3-0.4",
    "n": 36293,
    "mean_p": 0.3715,
    "observed": 0.4132
   },
   {
    "year": "2023",
    "target": "stop",
    "bin": "0.4-0.5",
    "n": 38469,
    "mean_p": 0.4531,
    "observed": 0.459
   },
   {
    "year": "2023",
    "target": "stop",
    "bin": "0.5-0.6",
    "n": 27391,
    "mean_p": 0.5431,
    "observed": 0.5115
   },
   {
    "year": "2023",
    "target": "stop",
    "bin": "0.6-0.7",
    "n": 3732,
    "mean_p": 0.6232,
    "observed": 0.5692
   },
   {
    "year": "2023",
    "target": "stop",
    "bin": "0.7-0.8",
    "n": 349,
    "mean_p": 0.7536,
    "observed": 0.5207
   },
   {
    "year": "2023",
    "target": "stop",
    "bin": "0.8-0.9",
    "n": 281,
    "mean_p": 0.85,
    "observed": 0.6758
   },
   {
    "year": "2023",
    "target": "stop",
    "bin": "0.9-1.0",
    "n": 803,
    "mean_p": 0.9676,
    "observed": 0.6825
   }
  ],
  "replay": [
   {
    "year": "2022",
    "use": "incumbent",
    "trades": 249,
    "net_pnl": -19803.24,
    "max_dd_pct": -85.46,
    "win_rate": 0.45
   },
   {
    "year": "2022",
    "use": "ml_er",
    "trades": 212,
    "net_pnl": -22857.04,
    "max_dd_pct": -93.58,
    "win_rate": 0.382
   },
   {
    "year": "2022",
    "use": "ml_er_pos",
    "trades": 125,
    "net_pnl": -22635.39,
    "max_dd_pct": -92.09,
    "win_rate": 0.376
   },
   {
    "year": "2022",
    "use": "ml_pstop",
    "trades": 141,
    "net_pnl": -21614.94,
    "max_dd_pct": -88.26,
    "win_rate": 0.454
   },
   {
    "year": "2022",
    "use": "debit_width",
    "trades": 276,
    "net_pnl": -23484.64,
    "max_dd_pct": -94.63,
    "win_rate": 0.409
   },
   {
    "year": "2023",
    "use": "incumbent",
    "trades": 282,
    "net_pnl": -1050.37,
    "max_dd_pct": -59.89,
    "win_rate": 0.418
   },
   {
    "year": "2023",
    "use": "ml_er",
    "trades": 264,
    "net_pnl": -6314.94,
    "max_dd_pct": -66.31,
    "win_rate": 0.466
   },
   {
    "year": "2023",
    "use": "ml_er_pos",
    "trades": 0,
    "net_pnl": 0.0,
    "max_dd_pct": 0.0,
    "win_rate": null
   },
   {
    "year": "2023",
    "use": "ml_pstop",
    "trades": 178,
    "net_pnl": -15431.76,
    "max_dd_pct": -80.43,
    "win_rate": 0.393
   },
   {
    "year": "2023",
    "use": "debit_width",
    "trades": 257,
    "net_pnl": -23678.63,
    "max_dd_pct": -95.33,
    "win_rate": 0.366
   }
  ]
 },
 "scope": {
  "arm_id": null
 },
 "shared_from_control": false
};

export const SHADOW_DETAIL = {
 "detail": {
  "as_of": "2026-10-28T09:00:00Z",
  "model": {
   "family": "credit",
   "label": "M2 · Candidate Outcome (Credit)",
   "family_kind": "outcome",
   "purpose": "P(win), E[R], P(stop), R quantiles and expected hold per credit candidate, calibrated out of fold.",
   "version": "m2-aaaaaaaaaaaa",
   "status": "shadow",
   "created": "2026-10-26T16:51:52-04:00",
   "source": "ML-2",
   "rule": "m2_promote",
   "rule_passed": false,
   "holdout_reads": 0,
   "stats": [
    {
     "label": "Promote Rule",
     "value": 1.0,
     "of": 2,
     "unit": "count",
     "text": null,
     "sub": "test years passed",
     "tone": "neutral"
    },
    {
     "label": "Log-Loss vs Logistic",
     "value": -0.070423,
     "of": null,
     "unit": "pct",
     "text": null,
     "sub": "win, 2023",
     "tone": "pos"
    },
    {
     "label": "E[R] IC",
     "value": 0.14,
     "of": null,
     "unit": "ratio",
     "text": null,
     "sub": "per day, 2023",
     "tone": "pos"
    }
   ]
  },
  "headline": [
   {
    "label": "Promote Rule",
    "value": 1.0,
    "of": 2,
    "unit": "count",
    "text": null,
    "sub": "test years passed",
    "tone": "neutral"
   },
   {
    "label": "Log-Loss vs Logistic",
    "value": -0.070423,
    "of": null,
    "unit": "pct",
    "text": null,
    "sub": "win, 2023",
    "tone": "pos"
   },
   {
    "label": "E[R] IC",
    "value": 0.14,
    "of": null,
    "unit": "ratio",
    "text": null,
    "sub": "per day, 2023",
    "tone": "pos"
   },
   {
    "label": "Win ECE",
    "value": 0.05,
    "of": null,
    "unit": "pts",
    "text": null,
    "sub": "calibrated, 2023",
    "tone": "neutral"
   },
   {
    "label": "Replay vs Incumbent",
    "value": 150.0,
    "of": null,
    "unit": "usd",
    "text": null,
    "sub": "net, ranked by E[R], 2023",
    "tone": "pos"
   },
   {
    "label": "Holdout Reads",
    "value": 0.0,
    "of": null,
    "unit": "count",
    "text": null,
    "sub": "dev only",
    "tone": "neutral"
   }
  ],
  "spec": [
   {
    "label": "Targets",
    "value": "win · stop · e_r"
   },
   {
    "label": "Profile",
    "value": "Cash Credit"
   },
   {
    "label": "Rows",
    "value": "1,000 train rows of 5,000 ledger rows"
   },
   {
    "label": "Train Window",
    "value": "2020-07-01 → 2023-12-31"
   },
   {
    "label": "Validation",
    "value": "walk-forward · test 2022, 2023"
   },
   {
    "label": "Baselines",
    "value": "logistic · managed PoP"
   },
   {
    "label": "Calibration",
    "value": "isotonic, out of fold"
   },
   {
    "label": "Params",
    "value": "lr 0.05 · 2 grid points"
   },
   {
    "label": "Features",
    "value": "3 (Candidate Ledger 1 · Ticker Options 1 · Ticker Price 1)"
   },
   {
    "label": "Promote Rule",
    "value": "beats logistic and managed PoP on log-loss, per-day E[R] IC > 0 (NW t > 2), replay beats the incumbent on net and max DD, both years"
   },
   {
    "label": "Lineage",
    "value": "schema 45ba7c64e725 · data dddddddddddddddd · code cccccccccc"
   }
  ],
  "feature_groups": {
   "candidate_ledger": 1,
   "ticker_options": 1,
   "ticker_price": 1
  },
  "importance": [
   {
    "target": "win",
    "kind": "gain",
    "rows": [
     [
      "ret_5d",
      0.6
     ],
     [
      "iv_rank",
      0.4
     ]
    ]
   },
   {
    "target": "stop",
    "kind": "gain",
    "rows": [
     [
      "iv_rank",
      1.0
     ]
    ]
   },
   {
    "target": "win",
    "kind": "permutation",
    "rows": [
     [
      "iv_rank",
      0.003
     ],
     [
      "ret_5d",
      -0.001
     ]
    ]
   }
  ],
  "regimes": [
   {
    "regime": "Trend · Bear",
    "target": "Win",
    "year": "2023",
    "n": 250,
    "rel": null,
    "cov_err": null,
    "ece": 0.13,
    "er_ic": 0.13
   },
   {
    "regime": "Trend · Bull",
    "target": "Win",
    "year": "2023",
    "n": 190,
    "rel": null,
    "cov_err": null,
    "ece": 0.07,
    "er_ic": 0.12
   }
  ],
  "loss": [
   {
    "group": "All",
    "label": "Logistic",
    "year": "2022",
    "rel": -0.014085
   },
   {
    "group": "All",
    "label": "Managed PoP",
    "year": "2022",
    "rel": -0.027778
   },
   {
    "group": "Stocks",
    "label": "Logistic",
    "year": "2022",
    "rel": -0.014085
   },
   {
    "group": "Stocks",
    "label": "Managed PoP",
    "year": "2022",
    "rel": -0.027778
   },
   {
    "group": "ETFs",
    "label": "Logistic",
    "year": "2022",
    "rel": -0.014085
   },
   {
    "group": "ETFs",
    "label": "Managed PoP",
    "year": "2022",
    "rel": -0.027778
   },
   {
    "group": "All",
    "label": "Logistic",
    "year": "2023",
    "rel": -0.070423
   },
   {
    "group": "All",
    "label": "Managed PoP",
    "year": "2023",
    "rel": -0.083333
   },
   {
    "group": "Stocks",
    "label": "Logistic",
    "year": "2023",
    "rel": -0.070423
   },
   {
    "group": "Stocks",
    "label": "Managed PoP",
    "year": "2023",
    "rel": -0.083333
   },
   {
    "group": "ETFs",
    "label": "Logistic",
    "year": "2023",
    "rel": -0.070423
   },
   {
    "group": "ETFs",
    "label": "Managed PoP",
    "year": "2023",
    "rel": -0.083333
   }
  ],
  "versions": [
   {
    "version": "m2-aaaaaaaaaaaa",
    "status": "shadow",
    "created": "2026-10-26T16:51:52-04:00",
    "rule_passed": false,
    "source": "ML-2",
    "train_s": 233.5,
    "note": null
   }
  ],
  "cost": {
   "train_s": 233.5,
   "folds_s": 141.3,
   "final_s": 31.3,
   "predict_s": null,
   "predict_rows": null,
   "predict_all_s": null,
   "size_bytes": null,
   "boosters": null,
   "peak_rss_mb": 8259.0
  },
  "live": {
   "day": "2026-10-27",
   "serving": "m2-aaaaaaaaaaaa",
   "status": null,
   "pct_scored": 95.0,
   "candidates": 200,
   "latency_ms_p95": 41,
   "calibration": "insufficient",
   "calibration_n": 4,
   "brier": null,
   "brier_base": null,
   "drift_status": "ok",
   "worst_psi": 0.31,
   "worst_psi_feature": "iv_rank",
   "drift_warn": 1,
   "score_psi": 0.05,
   "parity": "ok",
   "last_retrain": "2026-10-01"
  },
  "family_kind": "outcome",
  "rule": [
   {
    "year": "2022",
    "ll_model": 0.7,
    "ll_logistic": 0.71,
    "ll_managed_pop": 0.72,
    "beats_logistic": true,
    "beats_managed_pop": true,
    "er_ic": 0.08,
    "er_ic_t": 1.7,
    "ic_pass": false,
    "replay_pass": false,
    "net_incumbent": -100.0,
    "net_model": -200.0,
    "dd_incumbent": -50.0,
    "dd_model": -60.0,
    "passed": false
   },
   {
    "year": "2023",
    "ll_model": 0.66,
    "ll_logistic": 0.67,
    "ll_managed_pop": 0.69,
    "beats_logistic": true,
    "beats_managed_pop": true,
    "er_ic": 0.14,
    "er_ic_t": 14.4,
    "ic_pass": true,
    "replay_pass": true,
    "net_incumbent": -100.0,
    "net_model": 50.0,
    "dd_incumbent": -50.0,
    "dd_model": -40.0,
    "passed": true
   }
  ],
  "reliability": [
   {
    "year": "2023",
    "target": "win",
    "bin": "0.0-0.1",
    "n": 30,
    "mean_p": 0.06,
    "observed": 0.1
   },
   {
    "year": "2023",
    "target": "win",
    "bin": "0.5-0.6",
    "n": 50,
    "mean_p": 0.55,
    "observed": 0.52
   }
  ],
  "replay": [
   {
    "year": "2023",
    "use": "incumbent",
    "trades": 249,
    "net_pnl": -100.0,
    "max_dd_pct": -50.0,
    "win_rate": 0.45
   },
   {
    "year": "2023",
    "use": "ml_er",
    "trades": 212,
    "net_pnl": 50.0,
    "max_dd_pct": -40.0,
    "win_rate": 0.5
   }
  ]
 },
 "scope": {
  "arm_id": null
 },
 "shared_from_control": false
};

export const ML_STUDIES = {
 "as_of": "2026-10-28T09:00:00Z",
 "items": [
  {
   "study_id": "ML-4",
   "title": "Drop the event group from M1 (noise)",
   "hypothesis": "Drop the event group from M1 (noise)",
   "family": "m1",
   "family_label": "M1 · Move & Vol",
   "change_kind": "features",
   "status": "queued",
   "retired": false,
   "incumbent": "m1-53833784f276",
   "variants": 1,
   "steps": [
    {
     "step": "proposed",
     "label": "Proposed",
     "state": "done",
     "note": null
    },
    {
     "step": "approved",
     "label": "Approved",
     "state": "done",
     "note": null
    },
    {
     "step": "dev_2022",
     "label": "Dev 2022",
     "state": "pending",
     "note": null
    },
    {
     "step": "dev_2023",
     "label": "Dev 2023",
     "state": "pending",
     "note": null
    },
    {
     "step": "verdict",
     "label": "Verdict",
     "state": "pending",
     "note": null
    },
    {
     "step": "shadow",
     "label": "Shadow",
     "state": "pending",
     "note": null
    },
    {
     "step": "champion",
     "label": "Champion",
     "state": "pending",
     "note": null
    }
   ],
   "stage": "Approved",
   "verdict": null,
   "version": null,
   "evidence": [],
   "nights_needed": null,
   "runtime_s": null,
   "eta_s": 12000.0,
   "queue_position": 2,
   "created": "2026-10-27",
   "last_at": null
  },
  {
   "study_id": "ML-5",
   "title": "Rolling conformal window for M1 vol",
   "hypothesis": "A rolling 252-session CQR window fixes 2022 vol coverage",
   "family": "m1",
   "family_label": "M1 · Move & Vol",
   "change_kind": "calibration",
   "status": "proposed",
   "retired": false,
   "incumbent": "champion",
   "variants": 1,
   "steps": [
    {
     "step": "proposed",
     "label": "Proposed",
     "state": "done",
     "note": null
    },
    {
     "step": "approved",
     "label": "Approved",
     "state": "pending",
     "note": null
    },
    {
     "step": "dev_2022",
     "label": "Dev 2022",
     "state": "pending",
     "note": null
    },
    {
     "step": "dev_2023",
     "label": "Dev 2023",
     "state": "pending",
     "note": null
    },
    {
     "step": "verdict",
     "label": "Verdict",
     "state": "pending",
     "note": null
    },
    {
     "step": "shadow",
     "label": "Shadow",
     "state": "pending",
     "note": null
    },
    {
     "step": "champion",
     "label": "Champion",
     "state": "pending",
     "note": null
    }
   ],
   "stage": "Proposed",
   "verdict": null,
   "version": null,
   "evidence": [],
   "nights_needed": 2,
   "runtime_s": null,
   "eta_s": null,
   "queue_position": null,
   "created": "2026-10-25",
   "last_at": null
  },
  {
   "study_id": "ML-2",
   "title": "Monotone constraints on rorc_day cut credit log-loss",
   "hypothesis": "Monotone constraints on rorc_day cut credit log-loss",
   "family": "credit",
   "family_label": "M2 · Candidate Outcome (Credit)",
   "change_kind": "tuning",
   "status": "concluded",
   "retired": false,
   "incumbent": "m2-05fa82807a93",
   "variants": 2,
   "steps": [
    {
     "step": "proposed",
     "label": "Proposed",
     "state": "done",
     "note": null
    },
    {
     "step": "approved",
     "label": "Approved",
     "state": "done",
     "note": null
    },
    {
     "step": "dev_2022",
     "label": "Dev 2022",
     "state": "done",
     "note": "pass"
    },
    {
     "step": "dev_2023",
     "label": "Dev 2023",
     "state": "done",
     "note": "pass"
    },
    {
     "step": "verdict",
     "label": "Verdict",
     "state": "done",
     "note": "pass"
    },
    {
     "step": "shadow",
     "label": "Shadow",
     "state": "done",
     "note": null
    },
    {
     "step": "champion",
     "label": "Champion",
     "state": "pending",
     "note": null
    }
   ],
   "stage": "Shadow",
   "verdict": "pass",
   "version": "m2-aaaaaaaaaaaa",
   "evidence": [
    "Dev 2022 · pass · log-loss −2% vs incumbent, E[R] IC 0.09 · best of 2",
    "Dev 2023 · pass · log-loss −3% vs incumbent, E[R] IC 0.14 · best of 2",
    "Verdict · pass · m2-aaaaaaaaaaaa → Shadow"
   ],
   "nights_needed": null,
   "runtime_s": 3400.0,
   "eta_s": null,
   "queue_position": null,
   "created": "2026-10-20",
   "last_at": "2026-10-23T02:00:00Z"
  }
 ],
 "proposed": 1,
 "queued": 1,
 "running": null,
 "window_start": "21:30",
 "scope": {
  "arm_id": null
 },
 "shared_from_control": false
};

export const ML2_DETAIL = {
 "as_of": "2026-10-28T09:00:00Z",
 "study": {
  "study_id": "ML-2",
  "title": "Monotone constraints on rorc_day cut credit log-loss",
  "hypothesis": "Monotone constraints on rorc_day cut credit log-loss",
  "family": "credit",
  "family_label": "M2 · Candidate Outcome (Credit)",
  "change_kind": "tuning",
  "status": "concluded",
  "retired": false,
  "incumbent": "m2-05fa82807a93",
  "variants": 2,
  "steps": [
   {
    "step": "proposed",
    "label": "Proposed",
    "state": "done",
    "note": null
   },
   {
    "step": "approved",
    "label": "Approved",
    "state": "done",
    "note": null
   },
   {
    "step": "dev_2022",
    "label": "Dev 2022",
    "state": "done",
    "note": "pass"
   },
   {
    "step": "dev_2023",
    "label": "Dev 2023",
    "state": "done",
    "note": "pass"
   },
   {
    "step": "verdict",
    "label": "Verdict",
    "state": "done",
    "note": "pass"
   },
   {
    "step": "shadow",
    "label": "Shadow",
    "state": "done",
    "note": null
   },
   {
    "step": "champion",
    "label": "Champion",
    "state": "pending",
    "note": null
   }
  ],
  "stage": "Shadow",
  "verdict": "pass",
  "version": "m2-aaaaaaaaaaaa",
  "evidence": [
   "Dev 2022 · pass · log-loss −2% vs incumbent, E[R] IC 0.09 · best of 2",
   "Dev 2023 · pass · log-loss −3% vs incumbent, E[R] IC 0.14 · best of 2",
   "Verdict · pass · m2-aaaaaaaaaaaa → Shadow"
  ],
  "nights_needed": null,
  "runtime_s": 3400.0,
  "eta_s": null,
  "queue_position": null,
  "created": "2026-10-20",
  "last_at": "2026-10-23T02:00:00Z"
 },
 "spec": [
  {
   "label": "Hypothesis",
   "value": "Monotone constraints on rorc_day cut credit log-loss"
  },
  {
   "label": "Family",
   "value": "M2 · Candidate Outcome (Credit)"
  },
  {
   "label": "Change",
   "value": "Tuning"
  },
  {
   "label": "Incumbent",
   "value": "m2-05fa82807a93"
  },
  {
   "label": "Challenger",
   "value": "monotone.rorc_day: 1"
  },
  {
   "label": "Grid",
   "value": "2 configs"
  },
  {
   "label": "Decision Rule",
   "value": "m2_promote · pre-registered, unchanged"
  },
  {
   "label": "Holdout",
   "value": "never read by a study; promotion to champion is owner-only"
  },
  {
   "label": "If It Passes",
   "value": "registers as shadow (live scoring, no decision change)"
  },
  {
   "label": "Spec File",
   "value": "config/ml_studies/ML-2.yaml"
  }
 ],
 "overlay": {
  "monotone": {
   "rorc_day": 1
  }
 },
 "grid": [
  {
   "num_leaves": 15
  },
  {
   "num_leaves": 31
  }
 ],
 "rule": "m2_promote",
 "spec_file": "config/ml_studies/ML-2.yaml",
 "expected_effect": null,
 "scope": {
  "arm_id": null
 },
 "shared_from_control": false
};

export const ML5_DETAIL = {
 "as_of": "2026-10-28T09:00:00Z",
 "study": {
  "study_id": "ML-5",
  "title": "Rolling conformal window for M1 vol",
  "hypothesis": "A rolling 252-session CQR window fixes 2022 vol coverage",
  "family": "m1",
  "family_label": "M1 · Move & Vol",
  "change_kind": "calibration",
  "status": "proposed",
  "retired": false,
  "incumbent": "champion",
  "variants": 1,
  "steps": [
   {
    "step": "proposed",
    "label": "Proposed",
    "state": "done",
    "note": null
   },
   {
    "step": "approved",
    "label": "Approved",
    "state": "pending",
    "note": null
   },
   {
    "step": "dev_2022",
    "label": "Dev 2022",
    "state": "pending",
    "note": null
   },
   {
    "step": "dev_2023",
    "label": "Dev 2023",
    "state": "pending",
    "note": null
   },
   {
    "step": "verdict",
    "label": "Verdict",
    "state": "pending",
    "note": null
   },
   {
    "step": "shadow",
    "label": "Shadow",
    "state": "pending",
    "note": null
   },
   {
    "step": "champion",
    "label": "Champion",
    "state": "pending",
    "note": null
   }
  ],
  "stage": "Proposed",
  "verdict": null,
  "version": null,
  "evidence": [],
  "nights_needed": 2,
  "runtime_s": null,
  "eta_s": null,
  "queue_position": null,
  "created": "2026-10-25",
  "last_at": null
 },
 "spec": [
  {
   "label": "Hypothesis",
   "value": "A rolling 252-session CQR window fixes 2022 vol coverage"
  },
  {
   "label": "Family",
   "value": "M1 · Move & Vol"
  },
  {
   "label": "Change",
   "value": "Calibration"
  },
  {
   "label": "Incumbent",
   "value": "champion"
  },
  {
   "label": "Challenger",
   "value": "calibration.window: 252"
  },
  {
   "label": "Grid",
   "value": "1 config · no hyper-parameter search"
  },
  {
   "label": "Decision Rule",
   "value": "m1_keep · pre-registered, unchanged"
  },
  {
   "label": "Holdout",
   "value": "never read by a study; promotion to champion is owner-only"
  },
  {
   "label": "If It Passes",
   "value": "registers as shadow (live scoring, no decision change)"
  }
 ],
 "overlay": {
  "calibration": {
   "window": 252
  }
 },
 "grid": [],
 "rule": "m1_keep",
 "spec_file": null,
 "expected_effect": null,
 "scope": {
  "arm_id": null
 },
 "shared_from_control": false
};
