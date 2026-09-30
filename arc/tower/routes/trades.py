"""``GET /api/trades``, ``/api/trades/filters``, ``/api/trades/{hash}``, ``/api/search`` (E8.7b)."""

from __future__ import annotations

import datetime as _dt  # noqa: TC003 - FastAPI reads annotations at runtime
from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query

from arc.tower.api import TowerError
from arc.tower.data_trades import (
    MAX_PAGE_SIZE,
    DatePreset,
    SearchResponse,
    SortKey,
    TradeDetail,
    TradeFilterOptions,
    TradeFilters,
    TradeListResponse,
    load_filter_options,
    load_trade,
    load_trades,
    search,
)
from arc.tower.routes.deps import Conn, Tower  # noqa: TC001 - FastAPI dependencies
from arc.tower.schemas import ErrorResponse

__all__ = ["router"]

router = APIRouter(tags=["trades"], responses={503: {"model": ErrorResponse}})

Multi = Annotated[list[str] | None, Query()]


def _split(values: list[str] | None) -> list[str]:
    """Accept both ``?ticker=A&ticker=B`` and ``?ticker=A,B``."""
    out: list[str] = []
    for v in values or []:
        out += [x.strip() for x in v.split(",") if x.strip()]
    return out


@router.get("/trades", response_model=TradeListResponse)
def trades(  # noqa: PLR0913 - one query parameter per filter
    cfg: Tower,
    conn: Conn,
    date: Annotated[DatePreset, Query()] = "all",
    date_from: Annotated[_dt.date | None, Query()] = None,
    date_to: Annotated[_dt.date | None, Query()] = None,
    ticker: Multi = None,
    kind: Multi = None,
    structure: Multi = None,
    stage: Multi = None,
    exit_reason: Multi = None,
    reason_code: Multi = None,
    min_net_ev: Annotated[float | None, Query()] = None,
    min_pop: Annotated[float | None, Query(ge=0.0, le=1.0)] = None,
    account_profile: Multi = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    sort: Annotated[SortKey, Query()] = "time",
    dir: Annotated[Literal["asc", "desc"], Query()] = "desc",  # noqa: A002 - URL name
) -> TradeListResponse:
    """Every proposal (opens and closes), filtered, sorted and paged server-side, with
    the summary over the whole filter."""
    try:
        filters = TradeFilters.model_validate(
            {
                "date": date,
                "date_from": date_from,
                "date_to": date_to,
                "ticker": _split(ticker),
                "kind": _split(kind),
                "structure": _split(structure),
                "stage": _split(stage),
                "exit_reason": _split(exit_reason),
                "reason_code": _split(reason_code),
                "min_net_ev": min_net_ev,
                "min_pop": min_pop,
                "account_profile": _split(account_profile),
                "q": q or None,
            }
        )
    except ValueError as exc:
        raise TowerError(422, "invalid_request", str(exc)) from exc
    return load_trades(
        conn, filters, now=cfg.clock(), page=page, size=size, sort=sort, direction=dir
    )


@router.get("/trades/filters", response_model=TradeFilterOptions)
def trade_filters(cfg: Tower, conn: Conn) -> TradeFilterOptions:
    """Distinct values for the Trades filter dropdowns."""
    return load_filter_options(conn, now=cfg.clock())


@router.get(
    "/trades/{proposal_hash}",
    response_model=TradeDetail,
    responses={404: {"model": ErrorResponse}},
)
def trade(
    cfg: Tower,
    conn: Conn,
    proposal_hash: Annotated[str, Path(min_length=4, max_length=128)],
) -> TradeDetail:
    """The full drill-down for one proposal (every section in one round trip)."""
    detail = load_trade(conn, proposal_hash, now=cfg.clock())
    if detail is None:
        raise TowerError(404, "not_found", f"no trade {proposal_hash!r}")
    return detail


@router.get("/search", response_model=SearchResponse)
def global_search(
    cfg: Tower,
    conn: Conn,
    q: Annotated[str, Query(max_length=200)] = "",
) -> SearchResponse:
    """Resolve a ticker / hash prefix / run id / chain id / structure id to routes."""
    return search(conn, q, now=cfg.clock())
