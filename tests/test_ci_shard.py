"""D134: CI shards partition the suite: every test lands in exactly one shard."""

from __future__ import annotations

import pytest

from tests.ci_shard import ENV_VAR, current_shard, parse_shard, shard_of


def test_parse_shard_accepts_i_of_n_and_unset() -> None:
    assert parse_shard(None) is None
    assert parse_shard("") is None
    assert parse_shard("1/3") == (1, 3)
    assert parse_shard("3/3") == (3, 3)


@pytest.mark.parametrize("bad", ["0/3", "4/3", "1/0", "3", "a/b", "1/2/3"])
def test_parse_shard_rejects_malformed_values(bad: str) -> None:
    with pytest.raises(ValueError, match=ENV_VAR):
        parse_shard(bad)


def test_every_node_id_lands_in_exactly_one_shard_and_spreads() -> None:
    ids = [f"tests/test_x.py::test_case[{i}]" for i in range(3000)]
    counts = {i: 0 for i in (1, 2, 3)}
    for nodeid in ids:
        s = shard_of(nodeid, 3)
        assert s == shard_of(nodeid, 3)  # stable
        counts[s] += 1
    assert sum(counts.values()) == len(ids)
    assert min(counts.values()) > 900  # roughly even (1000 each)


def test_current_shard_reads_the_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert current_shard() is None
    monkeypatch.setenv(ENV_VAR, "2/3")
    assert current_shard() == (2, 3)
