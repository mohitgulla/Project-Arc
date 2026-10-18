"""CI test sharding (D134): split the suite across parallel CI jobs.

``ARC_TEST_SHARD=<i>/<n>`` (1-based) keeps only the tests whose node id hashes to
shard ``i`` of ``n``. Unset means no sharding, so local runs see the whole suite.
The hash is crc32 of the node id: stable across runs, machines and xdist workers
(each worker collects the same set), and it spreads parametrized cases of one slow
test over different shards.
"""

from __future__ import annotations

import os
import zlib

ENV_VAR = "ARC_TEST_SHARD"


def parse_shard(value: str | None) -> tuple[int, int] | None:
    """``"2/3"`` → ``(2, 3)``; ``None``/empty → ``None``; anything else raises."""
    if not value:
        return None
    try:
        i_s, n_s = value.split("/")
        i, n = int(i_s), int(n_s)
    except ValueError as exc:
        raise ValueError(f"{ENV_VAR} must look like 1/3, got {value!r}") from exc
    if n < 1 or not 1 <= i <= n:
        raise ValueError(f"{ENV_VAR} must satisfy 1 <= i <= n, got {value!r}")
    return i, n


def shard_of(nodeid: str, n: int) -> int:
    """1-based shard a test node id belongs to."""
    return zlib.crc32(nodeid.encode()) % n + 1


def current_shard() -> tuple[int, int] | None:
    return parse_shard(os.environ.get(ENV_VAR))
