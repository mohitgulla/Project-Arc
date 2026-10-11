"""Parquet cache of built candidate menus (E7.10, D84).

Building a menu (strike selection + the managed-exit Monte Carlo per candidate) is
the expensive part of a backtest. It depends only on the session's marked chain, the
menu specs, costs, exits and the MC knobs — never on ranking or portfolio settings —
so a run that only changes those reuses every cached menu.

Key = :class:`MenuCacheKey` ``(ticker, day, menu config hash, marks mode, exit config
hash, MC paths, seed)`` plus the history ``provider``; the menu hash also covers the
content of the day's marked chain and trailing closes (:func:`frame_hash`). The file is
``<data-dir>/backtest_cache/menus/<ticker>/<day>/<key hash>.parquet``: one row per
candidate (``Candidate`` JSON, menu order) and the key JSON in the file metadata.
Invalidation is by key only: a different key is a different file; ``clear`` deletes.
"""

from __future__ import annotations

import datetime as dt  # noqa: TC003 - pydantic field type
import hashlib
import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from collections.abc import Sequence

    from arc.backtest.ranking import Candidate

__all__ = [
    "CacheStats",
    "MenuCache",
    "MenuCacheContext",
    "MenuCacheKey",
    "config_hash",
    "frame_hash",
]

DIR = Path("backtest_cache") / "menus"
_KEY_META = b"arc.menu_cache_key"
_SCHEMA = pa.schema([("i", pa.int32()), ("candidate", pa.string())])


def config_hash(*parts: object) -> str:
    """Stable sha256 (16 hex) of pydantic models / JSON-able values."""

    def norm(p: object) -> object:
        if isinstance(p, BaseModel):
            return p.model_dump(mode="json")
        if isinstance(p, (list, tuple)):
            return [norm(x) for x in p]
        if isinstance(p, dict):
            return {str(k): norm(v) for k, v in p.items()}
        return p

    blob = json.dumps([norm(p) for p in parts], sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def frame_hash(df: pd.DataFrame) -> str:
    """Content hash (16 hex) of a frame's values and index (row order matters)."""
    h = pd.util.hash_pandas_object(df, index=True).to_numpy()
    return hashlib.sha256(h.tobytes() + ",".join(map(str, df.columns)).encode()).hexdigest()[:16]


class MenuCacheKey(BaseModel):
    """What a cached menu was built from (every field changes the file)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticker: str
    day: dt.date
    menu_hash: str = Field(
        ..., description="specs + cost model + rate + stance/tilt + chain/closes content"
    )
    marks: str = Field(..., description="Mark mode and its knobs, e.g. quoted:0.35:drop")
    exit_hash: str = Field(..., description="Exit policies + MC model config")
    n_paths: int
    seed: int
    provider: str = "alpaca"

    def digest(self) -> str:
        return config_hash(self)


class MenuCacheContext(BaseModel):
    """The run-level parts of a :class:`MenuCacheKey` (the menu hash is per call)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    marks: str
    exit_hash: str
    n_paths: int
    seed: int
    provider: str = "alpaca"

    def key(self, ticker: str, day: dt.date, *, menu_hash: str) -> MenuCacheKey:
        return MenuCacheKey(
            ticker=ticker.upper(), day=day, menu_hash=menu_hash, **self.model_dump()
        )


class CacheStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: str
    files: int = 0
    bytes: int = 0
    tickers: int = 0


class MenuCache:
    """Get/put menus under ``<data_dir>/backtest_cache/menus``; counts hits and misses."""

    def __init__(self, data_dir: Path | str) -> None:
        self.root = Path(data_dir) / DIR
        self.hits = 0
        self.misses = 0

    def path_for(self, key: MenuCacheKey) -> Path:
        return self.root / key.ticker.upper() / key.day.isoformat() / f"{key.digest()}.parquet"

    def get(self, key: MenuCacheKey) -> list[Candidate] | None:
        from arc.backtest.ranking import Candidate

        p = self.path_for(key)
        if not p.is_file():
            self.misses += 1
            return None
        t = pq.read_table(p)
        meta = (t.schema.metadata or {}).get(_KEY_META)
        if meta is None or MenuCacheKey.model_validate_json(meta) != key:  # hash collision
            self.misses += 1
            return None
        self.hits += 1
        return [Candidate.model_validate_json(s) for s in t.column("candidate").to_pylist()]

    def put(self, key: MenuCacheKey, menu: Sequence[Candidate]) -> Path:
        p = self.path_for(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pydict(
            {"i": list(range(len(menu))), "candidate": [c.model_dump_json() for c in menu]},
            schema=_SCHEMA.with_metadata({_KEY_META: key.model_dump_json().encode()}),
        )
        tmp = p.with_suffix(f".{id(table)}.tmp")
        pq.write_table(table, tmp)
        tmp.replace(p)
        return p

    def stats(self) -> CacheStats:
        if not self.root.is_dir():
            return CacheStats(root=str(self.root))
        files = list(self.root.glob("*/*/*.parquet"))
        return CacheStats(
            root=str(self.root),
            files=len(files),
            bytes=sum(f.stat().st_size for f in files),
            tickers=len({f.parent.parent.name for f in files}),
        )

    def clear(self, ticker: str | None = None) -> int:
        """Delete every cached menu (or one ticker's); returns the files removed."""
        target = self.root / ticker.upper() if ticker else self.root
        if not target.is_dir():
            return 0
        n = len(list(target.rglob("*.parquet")))
        shutil.rmtree(target)
        return n
