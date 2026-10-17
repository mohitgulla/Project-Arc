// Real /api/backtests* bodies (BT-7, BT-1) captured from the research ledger on 2026-10-17.
import type { BacktestDetail, BacktestsList } from "./backtests";

export const BT_LIST = {
 "as_of": "2026-10-17T04:02:41.605518-04:00",
 "account_profile": "cash_debit",
 "items": [
  {
   "study_id": "BT-7",
   "hypothesis": "Re-run: regime v2's stance labels are not worse than v1's on net P&L and max drawdown in either profile.",
   "status": "done",
   "variants": 1,
   "profiles": [
    "margin",
    "cash_debit"
   ],
   "profile": "cash_debit",
   "ladder": [
    {
     "rung": "c125_dev",
     "label": "C125 Dev",
     "done": true
    },
    {
     "rung": "c500_dev",
     "label": "C500 Dev",
     "done": true
    },
    {
     "rung": "holdout",
     "label": "Holdout",
     "done": true
    }
   ],
   "stage": "C500 Holdout",
   "stage_phase": "holdout",
   "runs": 3,
   "last_run_at": "2026-10-16T14:12:43.440812-04:00",
   "ci_level": 0.9,
   "headline": {
    "arm": "t1",
    "pnl_diff": -923.23,
    "ci_lo": -47528.02,
    "ci_hi": 50214.07,
    "switch": false,
    "call": "keep control",
    "periods_won": 0,
    "periods_won_names": []
   }
  },
  {
   "study_id": "BT-1",
   "hypothesis": "On the cash_debit profile, ranking the menu by managed Net EV picks better trades than the incumbent debit_width ranker.",
   "status": "stopped_at_dev",
   "variants": 1,
   "profiles": [
    "cash_debit"
   ],
   "profile": "cash_debit",
   "ladder": [
    {
     "rung": "c125_dev",
     "label": "C125 Dev",
     "done": true
    },
    {
     "rung": "c500_dev",
     "label": "C500 Dev",
     "done": false
    },
    {
     "rung": "holdout",
     "label": "Holdout",
     "done": false
    }
   ],
   "stage": "C125 Dev",
   "stage_phase": "dev",
   "runs": 1,
   "last_run_at": "2026-10-11T02:11:50.161856-04:00",
   "ci_level": 0.9,
   "headline": {
    "arm": "t1",
    "pnl_diff": -2004.22,
    "ci_lo": -7240.89,
    "ci_hi": 2851.22,
    "switch": false,
    "call": "keep control",
    "periods_won": 0,
    "periods_won_names": []
   }
  }
 ],
 "queued": 0,
 "running": null,
 "window_start": "21:30",
 "skipped_rows": 0
} as unknown as BacktestsList;

export const BT7_DETAIL = {
 "as_of": "2026-10-17T04:02:41.700677-04:00",
 "account_profile": "cash_debit",
 "study": {
  "study_id": "BT-7",
  "hypothesis": "Re-run: regime v2's stance labels are not worse than v1's on net P&L and max drawdown in either profile.",
  "status": "done",
  "variants": 1,
  "profiles": [
   "margin",
   "cash_debit"
  ],
  "profile": "cash_debit",
  "ladder": [
   {
    "rung": "c125_dev",
    "label": "C125 Dev",
    "done": true
   },
   {
    "rung": "c500_dev",
    "label": "C500 Dev",
    "done": true
   },
   {
    "rung": "holdout",
    "label": "Holdout",
    "done": true
   }
  ],
  "stage": "C500 Holdout",
  "stage_phase": "holdout",
  "runs": 3,
  "last_run_at": "2026-10-16T14:12:43.440812-04:00",
  "ci_level": 0.9,
  "headline": {
   "arm": "t1",
   "pnl_diff": -923.23,
   "ci_lo": -47528.02,
   "ci_hi": 50214.07,
   "switch": false,
   "call": "keep control",
   "periods_won": 0,
   "periods_won_names": []
  }
 },
 "rule": "Use the incumbent ranker (margin credit_width, cash_debit debit_width). Keep v2 if, in both profiles, v2 is not worse than v1 on net P&L **and** max drawdown, **and** the upper bound of the 90 % block-bootstrap CI of the daily P&L difference (v2 \u2212 v1) is > 0. Roll back to v1 (regime.model: v1) if v2 is worse on both net P&L and drawdown in either profile. Otherwise keep v2, inconclusive.",
 "rule_kind": "regime_keep",
 "created": "2026-10-15",
 "arms": {
  "t1": {
   "ranking": {
    "backtest": {
     "sim": {
      "regime_model": "v1"
     }
    }
   }
  }
 },
 "runs": [
  {
   "at": "2026-10-15T23:30:39.713506-04:00",
   "phase": "dev",
   "universe": "c125",
   "stage": "C125 Dev",
   "start": "2020-01-01",
   "end": "2023-12-31",
   "overfit_warning": false,
   "runtime_s": null,
   "results": [
    {
     "profile": "cash_debit",
     "arms": [
      {
       "arm": "control",
       "trades": 585,
       "net_pnl": -24879.91,
       "return_pct": -99.52,
       "max_dd_pct": 99.74,
       "sortino": -0.284,
       "profit_factor": 0.87,
       "win_rate": 0.504,
       "avg_days_held": 14.7,
       "cost_share_of_gross": 6.399,
       "capacity_clipped_share": 0.354,
       "exit_reasons": {
        "dte_exit": 0.0376,
        "expiry": 0.0308,
        "profit_lock": 0.0581,
        "stop": 0.3949,
        "take_profit": 0.4786
       },
       "skips": {
        "screen": 71615,
        "blackout": 0,
        "no_candidate": 24610,
        "unsettled": 0,
        "sizing": 5693,
        "gate_per_underlying": 0,
        "gate_max_open_positions": 3633,
        "gate_daily_loss": 11245,
        "gate_max_gain": 0,
        "cash": 64,
        "capacity": 492,
        "no_fill": 196,
        "exit_delayed": 52
       }
      },
      {
       "arm": "t1",
       "trades": 743,
       "net_pnl": -24886.16,
       "return_pct": -99.54,
       "max_dd_pct": 99.91,
       "sortino": -0.362,
       "profit_factor": 0.929,
       "win_rate": 0.522,
       "avg_days_held": 14.2,
       "cost_share_of_gross": 1.732,
       "capacity_clipped_share": 0.497,
       "exit_reasons": {
        "dte_exit": 0.039,
        "expiry": 0.031,
        "profit_lock": 0.0767,
        "stop": 0.3634,
        "take_profit": 0.4899
       },
       "skips": {
        "screen": 71615,
        "blackout": 0,
        "no_candidate": 20940,
        "unsettled": 0,
        "sizing": 5637,
        "gate_per_underlying": 0,
        "gate_max_open_positions": 10790,
        "gate_daily_loss": 11649,
        "gate_max_gain": 0,
        "cash": 114,
        "capacity": 780,
        "no_fill": 284,
        "exit_delayed": 73
       }
      }
     ],
     "treatments": [
      {
       "arm": "t1",
       "pnl_diff": -6.25,
       "ci_lo": -101250.26,
       "ci_hi": 97741.95,
       "switch": false,
       "call": "keep control",
       "periods_won": 0,
       "periods_won_names": []
      }
     ]
    },
    {
     "profile": "margin",
     "arms": [
      {
       "arm": "control",
       "trades": 440,
       "net_pnl": -17294.35,
       "return_pct": -69.18,
       "max_dd_pct": 83.0,
       "sortino": -0.291,
       "profit_factor": 0.789,
       "win_rate": 0.732,
       "avg_days_held": 21.0,
       "cost_share_of_gross": 1.845,
       "capacity_clipped_share": 0.373,
       "exit_reasons": {
        "dte_exit": 0.1,
        "expiry": 0.1614,
        "stop": 0.0818,
        "take_profit": 0.6568
       },
       "skips": {
        "screen": 71615,
        "blackout": 739,
        "no_candidate": 33046,
        "unsettled": 0,
        "sizing": 1559,
        "gate_per_underlying": 0,
        "gate_max_open_positions": 2970,
        "gate_daily_loss": 4506,
        "gate_max_gain": 0,
        "cash": 0,
        "capacity": 1001,
        "no_fill": 227,
        "exit_delayed": 24
       }
      },
      {
       "arm": "t1",
       "trades": 450,
       "net_pnl": -17533.4,
       "return_pct": -70.13,
       "max_dd_pct": 83.83,
       "sortino": -0.319,
       "profit_factor": 0.784,
       "win_rate": 0.718,
       "avg_days_held": 22.2,
       "cost_share_of_gross": 1.597,
       "capacity_clipped_share": 0.318,
       "exit_reasons": {
        "dte_exit": 0.1067,
        "expiry": 0.18,
        "stop": 0.1067,
        "take_profit": 0.6067
       },
       "skips": {
        "screen": 71615,
        "blackout": 596,
        "no_candidate": 32544,
        "unsettled": 0,
        "sizing": 1438,
        "gate_per_underlying": 0,
        "gate_max_open_positions": 3169,
        "gate_daily_loss": 4976,
        "gate_max_gain": 0,
        "cash": 0,
        "capacity": 893,
        "no_fill": 222,
        "exit_delayed": 20
       }
      }
     ],
     "treatments": [
      {
       "arm": "t1",
       "pnl_diff": -239.05,
       "ci_lo": -13509.87,
       "ci_hi": 13632.83,
       "switch": false,
       "call": "keep, inconclusive",
       "periods_won": 0,
       "periods_won_names": []
      }
     ]
    }
   ]
  },
  {
   "at": "2026-10-16T11:50:42.781128-04:00",
   "phase": "dev",
   "universe": "c500",
   "stage": "C500 Dev",
   "start": "2020-01-01",
   "end": "2023-12-31",
   "overfit_warning": false,
   "runtime_s": null,
   "results": [
    {
     "profile": "cash_debit",
     "arms": [
      {
       "arm": "control",
       "trades": 673,
       "net_pnl": -24931.49,
       "return_pct": -99.73,
       "max_dd_pct": 99.93,
       "sortino": -0.389,
       "profit_factor": 0.934,
       "win_rate": 0.484,
       "avg_days_held": 15.4,
       "cost_share_of_gross": 2.324,
       "capacity_clipped_share": 0.477,
       "exit_reasons": {
        "dte_exit": 0.0431,
        "expiry": 0.0461,
        "profit_lock": 0.055,
        "stop": 0.4012,
        "take_profit": 0.4547
       },
       "skips": {
        "screen": 291273,
        "blackout": 0,
        "no_candidate": 27396,
        "unsettled": 0,
        "sizing": 4406,
        "gate_per_underlying": 0,
        "gate_max_open_positions": 5127,
        "gate_daily_loss": 14927,
        "gate_max_gain": 0,
        "cash": 34,
        "capacity": 727,
        "no_fill": 205,
        "exit_delayed": 74
       }
      },
      {
       "arm": "t1",
       "trades": 721,
       "net_pnl": -24942.64,
       "return_pct": -99.77,
       "max_dd_pct": 99.92,
       "sortino": -0.682,
       "profit_factor": 0.922,
       "win_rate": 0.483,
       "avg_days_held": 14.4,
       "cost_share_of_gross": 1.831,
       "capacity_clipped_share": 0.549,
       "exit_reasons": {
        "dte_exit": 0.0485,
        "expiry": 0.0388,
        "profit_lock": 0.068,
        "stop": 0.3953,
        "take_profit": 0.4494
       },
       "skips": {
        "screen": 291273,
        "blackout": 0,
        "no_candidate": 24158,
        "unsettled": 0,
        "sizing": 8274,
        "gate_per_underlying": 0,
        "gate_max_open_positions": 11848,
        "gate_daily_loss": 13353,
        "gate_max_gain": 0,
        "cash": 87,
        "capacity": 753,
        "no_fill": 230,
        "exit_delayed": 71
       }
      }
     ],
     "treatments": [
      {
       "arm": "t1",
       "pnl_diff": -11.15,
       "ci_lo": -67335.75,
       "ci_hi": 78858.43,
       "switch": false,
       "call": "keep, inconclusive",
       "periods_won": 0,
       "periods_won_names": []
      }
     ]
    },
    {
     "profile": "margin",
     "arms": [
      {
       "arm": "control",
       "trades": 437,
       "net_pnl": -14004.78,
       "return_pct": -56.02,
       "max_dd_pct": 79.99,
       "sortino": -0.163,
       "profit_factor": 0.829,
       "win_rate": 0.716,
       "avg_days_held": 22.5,
       "cost_share_of_gross": 8.712,
       "capacity_clipped_share": 0.426,
       "exit_reasons": {
        "dte_exit": 0.119,
        "expiry": 0.1968,
        "stop": 0.0686,
        "take_profit": 0.6156
       },
       "skips": {
        "screen": 291273,
        "blackout": 544,
        "no_candidate": 39087,
        "unsettled": 0,
        "sizing": 1177,
        "gate_per_underlying": 0,
        "gate_max_open_positions": 3475,
        "gate_daily_loss": 5192,
        "gate_max_gain": 0,
        "cash": 0,
        "capacity": 893,
        "no_fill": 156,
        "exit_delayed": 22
       }
      },
      {
       "arm": "t1",
       "trades": 445,
       "net_pnl": -17756.02,
       "return_pct": -71.02,
       "max_dd_pct": 87.57,
       "sortino": -0.298,
       "profit_factor": 0.762,
       "win_rate": 0.724,
       "avg_days_held": 21.6,
       "cost_share_of_gross": 1.763,
       "capacity_clipped_share": 0.393,
       "exit_reasons": {
        "dte_exit": 0.1191,
        "expiry": 0.1528,
        "stop": 0.0966,
        "take_profit": 0.6315
       },
       "skips": {
        "screen": 291273,
        "blackout": 558,
        "no_candidate": 38476,
        "unsettled": 0,
        "sizing": 1442,
        "gate_per_underlying": 0,
        "gate_max_open_positions": 3069,
        "gate_daily_loss": 5762,
        "gate_max_gain": 0,
        "cash": 0,
        "capacity": 946,
        "no_fill": 158,
        "exit_delayed": 16
       }
      }
     ],
     "treatments": [
      {
       "arm": "t1",
       "pnl_diff": -3751.24,
       "ci_lo": -20306.15,
       "ci_hi": 14090.28,
       "switch": false,
       "call": "keep, inconclusive",
       "periods_won": 0,
       "periods_won_names": []
      }
     ]
    }
   ]
  },
  {
   "at": "2026-10-16T14:12:43.440812-04:00",
   "phase": "holdout",
   "universe": "c500",
   "stage": "C500 Holdout",
   "start": "2024-01-01",
   "end": "2026-10-15",
   "overfit_warning": false,
   "runtime_s": null,
   "results": [
    {
     "profile": "cash_debit",
     "arms": [
      {
       "arm": "control",
       "trades": 514,
       "net_pnl": -23843.49,
       "return_pct": -95.37,
       "max_dd_pct": 97.67,
       "sortino": -0.144,
       "profit_factor": 0.857,
       "win_rate": 0.504,
       "avg_days_held": 14.3,
       "cost_share_of_gross": 37.119,
       "capacity_clipped_share": 0.358,
       "exit_reasons": {
        "dte_exit": 0.0486,
        "expiry": 0.0467,
        "profit_lock": 0.0759,
        "stop": 0.3696,
        "take_profit": 0.4591
       },
       "skips": {
        "screen": 226930,
        "blackout": 0,
        "no_candidate": 18691,
        "unsettled": 221,
        "sizing": 2111,
        "gate_per_underlying": 0,
        "gate_max_open_positions": 3031,
        "gate_daily_loss": 9546,
        "gate_max_gain": 0,
        "cash": 31,
        "capacity": 311,
        "no_fill": 170,
        "exit_delayed": 86
       }
      },
      {
       "arm": "t1",
       "trades": 381,
       "net_pnl": -24766.72,
       "return_pct": -99.07,
       "max_dd_pct": 99.63,
       "sortino": -0.601,
       "profit_factor": 0.712,
       "win_rate": 0.496,
       "avg_days_held": 14.3,
       "cost_share_of_gross": 1.746,
       "capacity_clipped_share": 0.36,
       "exit_reasons": {
        "dte_exit": 0.0367,
        "expiry": 0.0472,
        "profit_lock": 0.0682,
        "stop": 0.3648,
        "take_profit": 0.4829
       },
       "skips": {
        "screen": 226930,
        "blackout": 0,
        "no_candidate": 17015,
        "unsettled": 715,
        "sizing": 7432,
        "gate_per_underlying": 0,
        "gate_max_open_positions": 3401,
        "gate_daily_loss": 8629,
        "gate_max_gain": 0,
        "cash": 31,
        "capacity": 243,
        "no_fill": 87,
        "exit_delayed": 41
       }
      }
     ],
     "treatments": [
      {
       "arm": "t1",
       "pnl_diff": -923.23,
       "ci_lo": -47528.02,
       "ci_hi": 50214.07,
       "switch": false,
       "call": "keep control",
       "periods_won": 0,
       "periods_won_names": []
      }
     ]
    },
    {
     "profile": "margin",
     "arms": [
      {
       "arm": "control",
       "trades": 316,
       "net_pnl": -3441.3,
       "return_pct": -13.77,
       "max_dd_pct": 56.9,
       "sortino": 0.132,
       "profit_factor": 0.962,
       "win_rate": 0.699,
       "avg_days_held": 24.7,
       "cost_share_of_gross": 1.367,
       "capacity_clipped_share": 0.522,
       "exit_reasons": {
        "dte_exit": 0.1266,
        "expiry": 0.2595,
        "stop": 0.0823,
        "take_profit": 0.5316
       },
       "skips": {
        "screen": 226930,
        "blackout": 613,
        "no_candidate": 25442,
        "unsettled": 255,
        "sizing": 510,
        "gate_per_underlying": 0,
        "gate_max_open_positions": 4424,
        "gate_daily_loss": 2815,
        "gate_max_gain": 0,
        "cash": 0,
        "capacity": 624,
        "no_fill": 153,
        "exit_delayed": 13
       }
      },
      {
       "arm": "t1",
       "trades": 317,
       "net_pnl": 31556.31,
       "return_pct": 126.23,
       "max_dd_pct": 36.98,
       "sortino": 0.891,
       "profit_factor": 1.363,
       "win_rate": 0.738,
       "avg_days_held": 25.1,
       "cost_share_of_gross": 0.318,
       "capacity_clipped_share": 0.609,
       "exit_reasons": {
        "dte_exit": 0.1388,
        "expiry": 0.2713,
        "stop": 0.0442,
        "take_profit": 0.5457
       },
       "skips": {
        "screen": 226930,
        "blackout": 428,
        "no_candidate": 26017,
        "unsettled": 256,
        "sizing": 344,
        "gate_per_underlying": 0,
        "gate_max_open_positions": 4587,
        "gate_daily_loss": 2318,
        "gate_max_gain": 0,
        "cash": 0,
        "capacity": 573,
        "no_fill": 134,
        "exit_delayed": 19
       }
      }
     ],
     "treatments": [
      {
       "arm": "t1",
       "pnl_diff": 34997.61,
       "ci_lo": 3535.57,
       "ci_hi": 69392.32,
       "switch": false,
       "call": "keep, inconclusive",
       "periods_won": 0,
       "periods_won_names": []
      }
     ]
    }
   ]
  }
 ],
 "linked": []
} as unknown as BacktestDetail;
