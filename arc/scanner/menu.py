"""One menu measure for every structure kind, ranked before the cut (PLAN D79; card E7.5b).

The scanner (:func:`arc.scanner.scan.scan`) ranks credit structures by credit ÷ width
and debit structures by ``ev_ratio`` in a separate group after them, then cuts the
menu to ``pipeline_scan_top``. Under a non-control ``exits.pipeline.menu_measure``
the pipeline instead asks the scanner for up to ``menu_pool_max`` candidates, runs the
E2.4 managed model on each and orders the pool on ONE dollar-based key for every kind
(:func:`rank_menu`) before cutting:

``managed_net_ev_full``  E2.4 managed Net EV after all costs, $ per unit
``rorc_day_full``        managed Net EV ÷ (max loss × expected days held)
``rorc_day_tilted``      ``rorc_day`` of the managed model run under a stance-signed
                         drift (:func:`arc.scanner.rank.tilted_drift`) instead of ``r``

The tilted model is a **ranking key only**: the card, the D41 Net EV floor and the
gate keep the untilted numbers (``models``), and the tilt is config
(``exits.pipeline.direction_tilt``), never an LLM number. Ordering is done by
:func:`arc.scanner.rank.rank` (no parallel ranker). Deterministic: no I/O, no clock.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from arc.exits.model import model_exits
from arc.scanner.rank import Ranker, RankInputs, rank, stance_sign, tilted_drift

if TYPE_CHECKING:
    from arc.exits import ExitConfig, ExitModelResult
    from arc.scanner.scan import ScanCandidate

__all__ = [
    "CONTROL",
    "MENU_MEASURE_RANKER",
    "MenuRankKey",
    "candidate_model",
    "rank_menu",
]

CONTROL = "control"

# menu measure -> the arc.scanner.rank ranker that orders the pool
MENU_MEASURE_RANKER: dict[str, Ranker] = {
    "managed_net_ev_full": Ranker.MANAGED_NET_EV,
    "rorc_day_full": Ranker.RORC_DAY,
    "rorc_day_tilted": Ranker.RORC_DAY_TILTED,
}


@dataclasses.dataclass(frozen=True)
class MenuRankKey:
    """Why a menu entry sits where it does: the measure, its value, the tilt used."""

    measure: str
    value: float | None
    tilt: float | None = None  # set only under rorc_day_tilted
    drift: float | None = None  # the annual drift of the tilted paths (r when untilted)


def candidate_model(
    c: ScanCandidate,
    spot: float,
    exits: ExitConfig,
    r: float,
    realized_vol: float | None,
    drift: float | None = None,
) -> ExitModelResult | None:
    """E2.4 static vs managed numbers for a scanner candidate (None without IV or DTE < 1).

    *drift* is only passed for the tilted ranking key; every number the card, the D41
    floor and the gate read comes from the ``drift=None`` call.
    """
    if c.atm_iv is None or c.dte < 1:
        return None
    return model_exits(
        c.structure,
        exits.policy_for(c.structure.kind),
        spot=spot,
        iv=c.atm_iv,
        r=r,
        cfg=exits.model,
        spreads=c.leg_spreads,
        realized_vol=realized_vol,
        drift=drift,
    )


def _tilted(
    c: ScanCandidate,
    base: ExitModelResult,
    *,
    stance: str,
    tilt: float,
    spot: float,
    exits: ExitConfig,
    r: float,
    realized_vol: float | None,
) -> tuple[ExitModelResult, float]:
    """(managed model under the stance-tilted drift, that drift); *base* when untilted.

    ``mu = r + sign(stance) × tilt × sigma_path / sqrt(T_hold)``: sigma_path is the vol
    the untilted paths moved at, T_hold their expected days held. Same seed as the
    untilted run, so the two differ only by the drift.
    """
    kind = c.structure.kind.value if c.structure.kind is not None else None
    mu = tilted_drift(
        r=r,
        sign=stance_sign(stance, kind),
        tilt=tilt,
        sigma=base.path_vol,
        hold_years=base.managed.expected_days_held / 365.0,
    )
    if mu == r:
        return base, r
    tm = candidate_model(c, spot, exits, r, realized_vol, drift=mu)
    return (base, r) if tm is None else (tm, mu)


def rank_menu(
    cands: list[ScanCandidate],
    models: dict[int, ExitModelResult],
    *,
    measure: str,
    top: int,
    stance: str,
    tilt: float,
    spot: float,
    exits: ExitConfig,
    r: float,
    realized_vol: float | None,
) -> tuple[list[ScanCandidate], dict[int, MenuRankKey]]:
    """Order a scanner pool on ONE key for every kind, then cut to *top*.

    *cands* is the scanner's pool in scanner order; *models* holds each candidate's
    untilted model by ``id(candidate)``. Credit and debit structures share one
    ordering (no group split). Ties break on scanner position. Candidates without a
    model (no IV, DTE < 1) or an undefined key (e.g. no max loss) follow, in scanner
    order. Returns the cut menu and each pool candidate's :class:`MenuRankKey`.
    """
    ranker = MENU_MEASURE_RANKER[measure]
    tilted = ranker is Ranker.RORC_DAY_TILTED
    by_key: dict[str, ScanCandidate] = {}
    keys: dict[int, MenuRankKey] = {}
    inputs: list[RankInputs] = []
    for i, c in enumerate(cands):
        base = models.get(id(c))
        if base is None:
            keys[id(c)] = MenuRankKey(measure, None)
            continue
        t_ev: float | None = None
        t_rorc: float | None = None
        mu: float | None = None
        if tilted:
            tm, mu = _tilted(
                c,
                base,
                stance=stance,
                tilt=tilt,
                spot=spot,
                exits=exits,
                r=r,
                realized_vol=realized_vol,
            )
            t_ev, t_rorc = tm.managed.net_ev, tm.rorc_day
        k = f"{i:05d}"  # scanner position: a deterministic, total tie-break
        by_key[k] = c
        inputs.append(
            RankInputs(
                key=k,
                credit=c.credit > 0,
                vertical=len(c.structure.legs) == 2,
                credit_width=c.credit_width,
                debit_width=None,
                ev_proxy=c.ev_proxy,
                ev_ratio=c.ev_ratio,
                managed_net_ev=base.managed.net_ev,
                managed_pop=base.managed.pop,
                rorc_day=base.rorc_day,
                vrp=base.vrp,
                managed_net_ev_tilted=t_ev,
                rorc_day_tilted=t_rorc,
                est_cost=None,
            )
        )
        value = {
            Ranker.MANAGED_NET_EV: base.managed.net_ev,
            Ranker.RORC_DAY: base.rorc_day,
            Ranker.RORC_DAY_TILTED: t_rorc,
        }[ranker]
        keys[id(c)] = MenuRankKey(measure, value, tilt if tilted else None, mu)
    ordered = [by_key[x.key] for x in rank(inputs, ranker)]
    seen = {id(c) for c in ordered}
    ordered += [c for c in cands if id(c) not in seen]
    return ordered[:top], keys
