"""E7.19 (D125) golden: a single-window margin book with ``expiry_mode: nearest`` is unchanged.

A single-window profile is one implicit ``default`` sleeve, so the sleeve work must
leave its book exactly as before. margin carries the cash_debit sleeves since D128, so
the run passes an explicit (20, 45) window, the one-window path (``sleeve_windows``
``override``). ``python -m tests.sim_margin_golden
--write`` regenerates ``tests/fixtures/sim_margin_golden.csv``; it was generated on
main without E7.19 (regenerated at 0a333be6, after D128/D131) from the live-shaped walk
(:func:`arc.backtest.sim.sim_ticker_from_chains` + :func:`arc.backtest.sim.run_sim`)
on the synthetic two-name history of ``tests/backtest_data_fixtures``. The file holds
the ``trades.csv`` text main wrote for that run. The test compares the parsed rows
with :func:`tests.golden_compare.assert_json_close` (floats to 1e-12, Linux CI BLAS)
after dropping the one column E7.19 adds, ``sleeve`` (always ``default`` for margin).
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd

from tests.backtest_data_fixtures import (
    R,
    sessions,
    spot_path,
    synth_rows,
    write_history,
    write_underlying,
    write_universe,
)

REPO = Path(__file__).resolve().parent.parent
GOLDEN = Path(__file__).parent / "fixtures" / "sim_margin_golden.csv"
N = 70
DAYS = sessions(dt.date(2025, 3, 3), N)
DECIDE = DAYS[:30]
WINDOW = (20, 45)


def trades_csv(root: Path) -> str:
    """Build the fixture under *root*, walk margin / credit_width, return trades.csv text."""
    from arc.backtest.costs import load_cost_model
    from arc.backtest.data import DataSettings, HistoryView
    from arc.backtest.ranking import load_ranking_file
    from arc.backtest.sim import DayInfo, SimSettings, run_sim, sim_ticker_from_chains
    from arc.backtest.strategies import ExpiryMode
    from arc.config import ArcSettings
    from arc.exits.policy import load_exit_config
    from arc.scanner.rank import Ranker, RankFilters

    spots = spot_path(N)
    names = ("AAA", "ZZZ")
    for i, t in enumerate(names):
        px = [s * (1 + 0.1 * i) for s in spots]
        write_history(root, t, synth_rows(t, DAYS, px, oi=400.0))
        write_underlying(root, t, DAYS, px)
    q = dt.date(2025, 1, 1)
    write_universe(root, list(names), [(q, t, True) for t in names])
    cost = load_cost_model(REPO / "config" / "costs.yaml")
    exits = load_exit_config()
    view = HistoryView(
        root,
        settings=DataSettings(pit=False),
        universe="c125",
        start=DAYS[0],
        end=DAYS[-1],
        cost=cost,
        flat_rate=R,
        min_iv_obs=20,
    )
    cfg = load_ranking_file()
    no_filters = RankFilters(enabled=False)
    menus = {
        p: [m.model_copy(update={"expiry_mode": ExpiryMode.NEAREST}) for m in ms]
        for p, ms in cfg.backtest.menus.items()
    }
    bt = cfg.backtest.model_copy(update={"n_paths": 200, "stance": "none", "menus": menus})
    cfg = cfg.model_copy(
        update={"backtest": bt, "ranking": cfg.ranking.model_copy(update={"filters": no_filters})}
    )
    settings = ArcSettings(max_alloc_pct=0.05).with_profile("margin")  # the suite's pin
    # E7.19's live entry filters and the D129 BP rule are pinned off (they post-date the
    # golden's main); a field main lacks is left out, so the same code regenerates it there
    pin = {"entry_filters": "none", "margin_buying_power": False}
    sim = SimSettings(
        momentum_size=0,
        regime_model="v1",
        equity=250_000,
        capacity="off",
        **{k: v for k, v in pin.items() if k in SimSettings.model_fields},
    )
    built = {}
    for i, t in enumerate(names):
        h = view.load(t)
        info = DayInfo(eligible=True, screen_pass=True, seed=False, liquidity=10.0 + 80 * i)
        built[t] = sim_ticker_from_chains(
            t,
            h.chains,
            h.closes,
            decision_days=DECIDE,
            days=dict.fromkeys(DAYS, info),
            cfg=cfg,
            exits=exits,
            cost=cost,
            settings={"margin": settings},
            windows={"margin": WINDOW},
            rankers={"margin": [Ranker.CREDIT_WIDTH]},
            stances=None,
            sim=sim,
        )
    run = run_sim(
        built,
        ranker=Ranker.CREDIT_WIDTH,
        profile="margin",
        settings=settings,
        sim=sim,
        filters=no_filters,
        vrp_threshold=0.0,
        sessions=DAYS,
        end=DECIDE[-1],
    )
    return run.trades.to_csv(index=False)


def records(text: str) -> list[dict[str, object]]:
    import io
    import json

    df = pd.read_csv(io.StringIO(text))
    return json.loads(str(df.to_json(orient="records", double_precision=15)))


def main(argv: list[str]) -> int:  # pragma: no cover - golden regeneration
    import tempfile

    if "--write" not in argv:
        sys.stderr.write("usage: python -m tests.sim_margin_golden --write\n")
        return 2
    with tempfile.TemporaryDirectory() as d:
        text = trades_csv(Path(d))
    GOLDEN.write_text(text)
    sys.stdout.write(f"wrote {len(records(text))} trades to {GOLDEN}\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
