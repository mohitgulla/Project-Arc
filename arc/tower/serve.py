"""``arc tower serve --v2`` (E8.7, D35): uvicorn on the Tailscale address.

Bind rules are the Streamlit tower's (:func:`arc.tower.net.resolve_bind_address`,
D29): a Tailscale ``100.64.0.0/10`` address or loopback, never ``0.0.0.0`` or a LAN
IP, and no start at all without one. uvicorn runs in a child process with the app
factory ``arc.tower.serve:app_from_env``; its settings travel in ``ARC_TOWER_*``
environment variables, the same ones the Streamlit page reads.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

__all__ = ["ENV_DB", "ENV_LOOKBACK", "ENV_REFRESH", "app_from_env", "uvicorn_command"]

ENV_DB = "ARC_TOWER_DB"
ENV_REFRESH = "ARC_TOWER_REFRESH"
ENV_LOOKBACK = "ARC_TOWER_LOOKBACK_DAYS"
FACTORY = "arc.tower.serve:app_from_env"


def uvicorn_command(address: str, port: int) -> list[str]:
    """The uvicorn argv: app factory, one worker, no reload, no proxy headers, no banner."""
    return [
        sys.executable,
        "-m",
        "uvicorn",
        FACTORY,
        "--factory",
        "--host",
        address,
        "--port",
        str(port),
        "--workers",
        "1",
        "--no-proxy-headers",
        "--no-server-header",
        "--no-use-colors",
        "--log-level",
        "info",
    ]


def app_from_env() -> FastAPI:
    """uvicorn factory: build the app from ``ARC_TOWER_*`` (set by ``arc tower serve --v2``)."""
    from arc.config import get_settings
    from arc.store.db import DEFAULT_DB_PATH
    from arc.tower.api import create_app

    settings = get_settings()
    db = os.environ.get(ENV_DB) or str(settings.db_path or DEFAULT_DB_PATH)
    return create_app(
        Path(db),
        settings,
        refresh_s=int(os.environ.get(ENV_REFRESH, "60")),
        lookback_days=int(os.environ.get(ENV_LOOKBACK, "7")),
    )
