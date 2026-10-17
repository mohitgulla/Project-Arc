"""Build the history feature store on disk (E27.1).

Layout: ``<out>/v<FEATURE_SCHEMA_VERSION>/date=<YYYY-MM-DD>.parquet``, one panel per
session, plus ``manifest.json``. Resumable: a date whose file exists is skipped, and each
file is written atomically (tmp + rename). A schema change writes to a new ``v<schema>``
directory, so old panels are never mixed with new ones.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import TYPE_CHECKING

import structlog

from arc.ml.features.adapters.history import history_inputs
from arc.ml.features.compute import compute_panel
from arc.ml.features.registry import FEATURES, panel_feature_names, schema_version
from arc.utils.calendar import sessions_between

if TYPE_CHECKING:
    import datetime as dt
    from collections.abc import Callable, Sequence
    from pathlib import Path

    import pandas as pd

log = structlog.get_logger(__name__)


def version_dir(out_root: Path, schema: str | None = None) -> Path:
    return out_root / f"v{schema or schema_version()}"


def date_path(vdir: Path, d: dt.date) -> Path:
    return vdir / f"date={d.isoformat()}.parquet"


def _chunks(dates: Sequence[dt.date]) -> list[list[dt.date]]:
    out: dict[int, list[dt.date]] = {}
    for d in dates:
        out.setdefault(d.year, []).append(d)
    return [out[y] for y in sorted(out)]


def write_manifest(vdir: Path, tickers: Sequence[str], universe: str) -> None:
    body = {
        "schema_version": vdir.name.removeprefix("v"),
        "features": panel_feature_names(),
        "registry": [f.model_dump(mode="json") for f in FEATURES],
        "universe": universe,
        "tickers_sha256": hashlib.sha256(",".join(sorted(tickers)).encode()).hexdigest(),
        "n_tickers": len(tickers),
    }
    vdir.mkdir(parents=True, exist_ok=True)
    (vdir / "manifest.json").write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")


def _write_atomic(df: pd.DataFrame, path: Path) -> None:
    tmp = path.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)


def build_store(
    data_dir: Path,
    out_root: Path,
    tickers: Sequence[str],
    start: dt.date,
    end: dt.date,
    *,
    etfs: frozenset[str] = frozenset(),
    members: dict[dt.date, frozenset[str]] | None = None,
    universe: str = "",
    progress: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Write every missing session panel in [start, end]; returns counts."""
    vdir = version_dir(out_root)
    write_manifest(vdir, tickers, universe)
    todo = [d for d in sessions_between(start, end) if not date_path(vdir, d).is_file()]
    rows = written = 0
    for chunk in _chunks(todo):
        inp = history_inputs(data_dir, tickers, chunk[0], chunk[-1], members=members, etfs=etfs)
        panel = compute_panel(inp, chunk, tickers)
        for d, g in panel.groupby("date", sort=True):
            _write_atomic(g.reset_index(drop=True), date_path(vdir, d))
            written += 1
            rows += len(g)
        log.info("ml.features.chunk", first=str(chunk[0]), last=str(chunk[-1]), rows=len(panel))
        if progress is not None:
            progress(f"{chunk[0]}..{chunk[-1]}: {len(panel)} rows")
    return {
        "dates_skipped": len(sessions_between(start, end)) - len(todo),
        "dates_written": written,
        "rows": rows,
    }
