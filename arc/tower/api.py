"""FastAPI app for control tower v2 (E8.7, D35): a read-only JSON API + the built SPA.

:func:`create_app` wires:

- ``/api/*``: GET-only JSON routes (:mod:`arc.tower.routes`). Each request opens
  the audit store with :func:`arc.tower.data.connect_ro` (``mode=ro`` +
  ``query_only``) and closes it when the response is sent. Every response
  carries ``as_of`` (ET); errors are ``{error, detail, as_of}``.
- ``/``: the SPA built by ``make web`` into ``arc/tower/static/``, with an
  ``index.html`` fallback for client-side routes. ``/api/*`` never falls back.

No CORS middleware (same origin only), no auth: the bind (Tailscale or loopback,
:mod:`arc.tower.net`) is the boundary, as with the Streamlit tower (OPS §5.6).
The tower never imports the broker, market data, personas, an LLM or Slack
(import-linter contract in ``pyproject.toml``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from arc.tower.schemas import ErrorResponse
from arc.utils.calendar import now_et

if TYPE_CHECKING:
    import datetime as _dt
    from collections.abc import Callable

    from arc.config import ArcSettings

__all__ = ["STATIC_DIR", "TowerConfig", "TowerError", "create_app"]

STATIC_DIR = Path(__file__).resolve().parent / "static"
API_PREFIX = "/api"


class TowerError(Exception):
    """A handled API failure, rendered as ``{error, detail, as_of}`` with *status*."""

    def __init__(self, status: int, error: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.error = error
        self.detail = detail


@dataclass(frozen=True)
class TowerConfig:
    """Per-app settings, kept on ``app.state.tower`` (no module globals)."""

    db_path: Path
    settings: ArcSettings | None = None
    refresh_s: int = 60
    lookback_days: int = 7
    static_dir: Path = STATIC_DIR
    routines_path: Path | None = None
    clock: Callable[[], _dt.datetime] = field(default=now_et)


def _error(status: int, error: str, detail: str, now: _dt.datetime) -> JSONResponse:
    body = ErrorResponse(error=error, detail=detail, as_of=now)
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"))


def _is_api(path: str) -> bool:
    return path == API_PREFIX or path.startswith(API_PREFIX + "/")


_HTTP_CODES = {
    404: "not_found",
    405: "method_not_allowed",
}


def create_app(
    db_path: Path | str,
    settings: ArcSettings | None = None,
    *,
    refresh_s: int = 60,
    lookback_days: int = 7,
    static_dir: Path | str | None = None,
    routines_path: Path | str | None = None,
    clock: Callable[[], _dt.datetime] = now_et,
) -> FastAPI:
    """The tower app over the audit store at *db_path* (opened read-only per request).

    *settings* is the base :class:`~arc.config.ArcSettings`; D26 overrides from the
    DB are applied on each ``/api/meta`` / ``/api/snapshot`` read, so a Slack
    config change reaches the tower without a restart.
    """
    from arc.tower.routes import meta, overview, snapshot, trades

    cfg = TowerConfig(
        db_path=Path(db_path).expanduser().resolve(),
        settings=settings,
        refresh_s=refresh_s,
        lookback_days=lookback_days,
        static_dir=Path(static_dir).resolve() if static_dir is not None else STATIC_DIR,
        routines_path=Path(routines_path) if routines_path is not None else None,
        clock=clock,
    )
    app = FastAPI(
        title="Arc control tower",
        description="Read-only view of the Arc audit store (D35). GET only.",
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=None,
    )
    app.state.tower = cfg

    @app.exception_handler(TowerError)
    async def _tower_error(_: Request, exc: TowerError) -> JSONResponse:
        return _error(exc.status, exc.error, exc.detail, cfg.clock())

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(exc.status_code, f"http_{exc.status_code}")
        return _error(exc.status_code, code, str(exc.detail), cfg.clock())

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _error(422, "invalid_request", str(exc.errors()), cfg.clock())

    app.include_router(meta.router, prefix=API_PREFIX)
    app.include_router(snapshot.router, prefix=API_PREFIX)
    app.include_router(overview.router, prefix=API_PREFIX)
    app.include_router(trades.router, prefix=API_PREFIX)

    @app.get(f"{API_PREFIX}/{{rest:path}}", include_in_schema=False)
    def _api_not_found(rest: str) -> JSONResponse:
        return _error(404, "not_found", f"no API route /api/{rest}", cfg.clock())

    @app.get("/{path:path}", include_in_schema=False, response_model=None)
    def _spa(path: str) -> FileResponse | PlainTextResponse:
        root = cfg.static_dir
        index = root / "index.html"
        if path:
            candidate = (root / path).resolve()
            if candidate.is_relative_to(root) and candidate.is_file():
                return FileResponse(candidate)
            if "." in Path(path).name:  # a missing asset, not a client route
                return PlainTextResponse("not found", status_code=404)
        if index.is_file():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return PlainTextResponse(
            "Arc tower v2: the web app is not built. Run `make web` (API: /api/health).",
            status_code=503,
        )

    return app
