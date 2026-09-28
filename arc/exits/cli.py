"""``arc exits model``: static (hold to expiry) vs managed-exit PoP / net EV (E2.4, D23)."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse
    from collections.abc import Sequence

    from arc.data.base import MarketDataProvider
    from arc.exits.model import ExitModelResult, TriggerLevels
    from arc.exits.policy import ExitPolicy
    from arc.scanner import ScanCandidate

__all__ = ["add_exits_parser", "format_result", "run_exits"]


def add_exits_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("exits", help="Exit policy + managed-exit model (E2.4)")
    esub = p.add_subparsers(dest="exits_command", required=True)
    m = esub.add_parser("model", help="Static vs managed PoP / net EV for a scanner structure")
    m.add_argument("ticker", nargs="?", default="SPY", help="Underlying (default SPY)")
    m.add_argument(
        "--fixture",
        default=None,
        metavar="PATH",
        help="Recorded chain JSON (offline). 'spy' = the bundled SPY recording.",
    )
    m.add_argument(
        "--strategy",
        default="iron_condor",
        choices=["iron_condor", "bull_put", "bear_call"],
        help="Scanner structure to model (default: the top-ranked iron condor)",
    )
    m.add_argument("--rank", type=int, default=1, help="Which ranked candidate (1 = best)")
    m.add_argument("--config", default=None, help="exits.yaml path (default config/exits.yaml)")
    m.add_argument("--paths", type=int, default=None, help="Override n_paths")
    m.add_argument("--seed", type=int, default=None, help="Override the seed")
    m.add_argument("--as-of", type=dt.date.fromisoformat, default=None, help="YYYY-MM-DD")
    m.add_argument(
        "--path-vol",
        choices=["realized_forecast", "iv"],
        default=None,
        help="Paths at the realised-vol forecast (mean HV20/HV60, config default) or at IV",
    )
    m.add_argument(
        "--no-compare", action="store_true", help="Skip the 2x-credit / no-stop comparison rows"
    )
    m.add_argument("--json", action="store_true", help="Emit the ExitModelResult as JSON")


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _money(x: float) -> str:
    return f"{'-' if x < 0 else '+'}${abs(x):,.2f}"


def _trigger(name: str, t: TriggerLevels | None) -> str:
    if t is None:
        return f"  {name:<12} none"
    where = []
    if t.underlying_down is not None:
        where.append(f"≈{t.underlying_down:.2f} (down)")
    if t.underlying_up is not None:
        where.append(f"≈{t.underlying_up:.2f} (up)")
    if not t.reachable:
        return (
            f"  {name:<12} close at ${t.close_price:.2f} {t.close_side} "
            f"(P&L {t.pnl:+.2f}/sh)  UNREACHABLE: beyond the structure's max gain/loss"
        )
    day = "" if t.median_day is None else f", median day {t.median_day:.0f}"
    return (
        f"  {name:<12} close at ${t.close_price:.2f} {t.close_side} "
        f"(P&L {t.pnl:+.2f}/sh)  p={_pct(t.probability)}"
        + (f"  underlying {' / '.join(where)}{day}" if where else "")
    )


def _row(label: str, pop: float, pop_gross: float, gross: float, net: float, days: float,
         per: float | None) -> str:  # fmt: skip
    return (
        f"  {label:<26}{_pct(pop):>10}{_pct(pop_gross):>11}{_money(gross):>11}{_money(net):>11}"
        f"{days:>7.1f}{'' if per is None else f'{per * 1e3:+.3f}':>12}"
    )


def format_result(
    c: ScanCandidate, r: ExitModelResult, compare: Sequence[tuple[str, ExitModelResult]] = ()
) -> str:
    """Human-readable static vs managed table; *compare* adds managed rows for other policies."""
    from arc.structures import parse_occ

    strikes = "/".join(
        f"{parse_occ(leg.occ_symbol).strike.normalize():f}" for leg in c.structure.legs
    )
    s, m = r.static, r.managed
    side = "credit" if r.entry_net < 0 else "debit"
    lines = [
        f"{c.ticker} {c.strategy.value} {strikes} exp {c.expiration} ({r.dte} DTE)  "
        f"spot {r.spot:.2f}  {side} {abs(r.entry_net):.2f}  "
        f"max loss ${float(c.structure.max_loss or 0):,.0f}",
        f"model {r.model}  marks at IV {_pct(r.iv_used)} ({r.iv_model.kind})  "
        f"paths at {_pct(r.path_vol)} ({r.path_vol_source})  r {r.r:.2%}  "
        f"paths {r.n_paths:,}  seed {r.seed}  PoP s.e. ±{_pct(r.pop_std_error)}",
        "ranking inputs: "
        f"managed net EV {_money(r.managed.net_ev)}  "
        f"rorc/day {'n/a' if r.rorc_day is None else f'{r.rorc_day * 100:+.3f}%'}  "
        f"VRP {'n/a (no realised-vol forecast)' if r.vrp is None else f'{r.vrp * 100:+.1f} pts'}",
        f"policy: {r.policy.summary()}",
        f"entry costs {_money(-r.entry_costs)} (slippage + commissions)",
        "",
        f"  {'':<26}{'PoP (net)':>10}{'PoP (mid)':>11}{'Gross EV':>11}{'Net EV':>11}"
        f"{'Days':>7}{'$/1kBP/day':>12}",
        f"  {'static (hold to exp.)':<26}{_pct(s.pop):>10}{_pct(s.pop_gross):>11}"
        f"{_money(s.gross_ev):>11}{_money(s.net_ev):>11}{r.dte:>7}"
        f"{'' if s.ev_per_bp_day is None else f'{s.ev_per_bp_day * 1e3:+.3f}':>12}",
        f"  {'managed (policy)':<26}{_pct(m.pop):>10}{_pct(m.pop_gross):>11}"
        f"{_money(m.gross_ev):>11}{_money(m.net_ev):>11}{m.expected_days_held:>7.1f}"
        f"{'' if m.ev_per_bp_day is None else f'{m.ev_per_bp_day * 1e3:+.3f}':>12}",
        *(
            _row(
                f"managed ({name})",
                x.managed.pop,
                x.managed.pop_gross,
                x.managed.gross_ev,
                x.managed.net_ev,
                x.managed.expected_days_held,
                x.managed.ev_per_bp_day,
            )
            + f"   stop p={_pct(x.managed.p_stop)}"
            for name, x in compare
        ),
        f"  analytic lognormal PoP (mid, hold to expiry): {_pct(s.pop_analytic)}"
        f"   scanner PoP {_pct(c.pop)}  scanner EV {_money(c.ev_proxy)}",
        "",
        f"  exit reasons: take profit {_pct(m.p_take_profit)}  stop {_pct(m.p_stop)}  "
        f"DTE exit {_pct(m.p_dte_exit)}  expiry {_pct(m.p_expiry)}",
        _trigger("take profit", r.take_profit),
        _trigger("stop", r.stop),
        "  DTE exit     "
        + (
            "none"
            if r.dte_exit_day is None
            else f"day {r.dte_exit_day} ({r.policy.close_at_dte} DTE left)"
        ),
    ]
    return "\n".join(lines) + "\n"


def run_exits(args: argparse.Namespace) -> int:
    from arc.config import get_settings
    from arc.exits import StopBasis, StopRule, load_exit_config, model_exits
    from arc.scanner import ScanParams, ScanStrategy, scan
    from arc.utils.calendar import now_et

    settings = get_settings()
    try:
        cfg = load_exit_config(args.config)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"arc exits: {exc}\n")
        return 2
    overrides = {
        k: v
        for k, v in (("n_paths", args.paths), ("seed", args.seed), ("path_vol", args.path_vol))
        if v is not None
    }
    model_cfg = cfg.model.model_copy(update=overrides)

    provider: MarketDataProvider
    ticker = args.ticker.upper()
    if args.fixture:
        from arc.data.recorded import SPY_CHAIN_FIXTURE, RecordedMarketData

        path = SPY_CHAIN_FIXTURE if args.fixture.lower() == "spy" else Path(args.fixture)
        recorded = RecordedMarketData.from_files(path)
        provider = recorded
        try:
            as_of = args.as_of or recorded.recording(ticker).as_of
        except KeyError as exc:
            sys.stderr.write(f"arc exits: {exc.args[0]}\n")
            return 2
    else:
        from arc.data.alpaca import AlpacaMarketData

        try:
            provider = AlpacaMarketData()
        except RuntimeError as exc:
            sys.stderr.write(f"arc exits: {exc} (or pass --fixture spy to run offline)\n")
            return 2
        as_of = args.as_of or now_et().date()

    params = ScanParams.from_settings(settings, strategies=[ScanStrategy(args.strategy)])
    res = scan(provider, ticker, params, as_of=as_of)
    if len(res.candidates) < args.rank:
        sys.stderr.write(f"arc exits: no {args.strategy} candidate #{args.rank} for {ticker}\n")
        return 1
    c = res.candidates[args.rank - 1]
    if c.atm_iv is None:
        sys.stderr.write("arc exits: no ATM IV for the expiration\n")
        return 1
    rv = _realized_vol(provider, ticker, as_of)
    policy = cfg.policy_for(c.structure.kind)

    def run(pol: ExitPolicy) -> ExitModelResult:
        return model_exits(
            c.structure,
            pol,
            spot=res.spot,
            iv=c.atm_iv,  # type: ignore[arg-type]  # checked above
            r=settings.scanner_risk_free_rate,
            cfg=model_cfg,
            spreads=c.leg_spreads,
            realized_vol=rv,
        )

    r = run(policy)
    if args.json:
        sys.stdout.write(r.model_dump_json(indent=2) + "\n")
        return 0
    compare: list[tuple[str, ExitModelResult]] = []
    if not args.no_compare and r.entry_net < 0:
        two_x = StopRule(basis=StopBasis.CREDIT_MULTIPLE, value=2.0)
        compare = [
            ("2x credit stop", run(policy.model_copy(update={"stop": two_x}))),
            ("no stop", run(policy.model_copy(update={"stop": None}))),
        ]
    sys.stdout.write(format_result(c, r, compare))
    return 0


def _realized_vol(provider: MarketDataProvider, ticker: str, as_of: dt.date) -> float | None:
    """mean(HV20, HV60) from daily history; None when history is too short or unavailable."""
    from arc.exits import realized_vol_forecast
    from arc.features.snapshot import build_snapshot_from_bars

    try:
        bars = provider.history_bars(ticker, as_of - dt.timedelta(days=400), as_of)
        vol = build_snapshot_from_bars(ticker, bars, as_of).vol
    except Exception:  # noqa: BLE001 - optional input; fall back to IV paths
        return None
    return realized_vol_forecast(vol.hv20, vol.hv60)
