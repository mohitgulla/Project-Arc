"""D131 (E28.5): correlation cluster cap — ``arc.gate.cluster`` and rule cluster_cap.

A new open on T fails when ``cluster_cap_max_names`` (2) *other* open names (positions +
reserved in-flight opens) have corr(T, U) > ``cluster_cap_corr_threshold`` (0.7); a pair
with no correlation counts. Baseline proposal (tests/test_gate.py): SPY bull put.
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest
from hypothesis import given
from hypothesis import strategies as st

from arc.config import ArcSettings
from arc.gate import MarketSnapshot, Portfolio, Position, RuleCode, derive
from arc.gate import rules as R
from arc.gate.cluster import ClusterCheck, cluster_check, corr_key
from tests.test_gate import LP, NOW, SP, acct, make_proposal, mkt, run


def cfg(**kw: object) -> ArcSettings:
    base: dict[str, object] = {
        "cluster_cap_enabled": True,
        "cluster_cap_corr_threshold": 0.7,
        "cluster_cap_max_names": 2,
    }
    base.update(kw)
    return ArcSettings(_env_file=None, account_profile="margin", **base)  # type: ignore[call-arg]


def book(*names: str, reserved: tuple[str, ...] = ()) -> Portfolio:
    return Portfolio(
        positions=[Position(underlying=n, max_loss=D("100")) for n in names],
        reserved_underlyings=reserved,
    )


def corrs(**pairs: float) -> dict[str, float]:
    """``corrs(QQQ=0.9)`` = SPY's correlation with QQQ."""
    return {corr_key("SPY", t): c for t, c in pairs.items()}


def check(pf: Portfolio, c: dict[str, float], **kw: object) -> list[R.Violation]:
    return R.check_cluster_cap(derive(make_proposal()), pf, mkt(correlations=c), cfg(**kw))


# ---------------------------------------------------------------------------
# arc.gate.cluster
# ---------------------------------------------------------------------------


def test_corr_key_is_sorted_and_upper() -> None:
    assert corr_key("qqq", "AAPL") == "AAPL|QQQ" == corr_key("AAPL", "qqq")


def test_cluster_check_counts_above_threshold_and_missing() -> None:
    c = {corr_key("NVDA", "AMD"): 0.85, corr_key("NVDA", "KO"): 0.2}
    chk = cluster_check("nvda", ["AMD", "KO", "AVGO", "NVDA"], c, threshold=0.7, max_names=2)
    assert chk == ClusterCheck("NVDA", ("AMD", "AVGO"), ("AVGO",), 2, 0.7)
    assert chk.blocked
    assert chk.describe() == "AMD, AVGO (no correlation stored for AVGO: counted)"


def test_cluster_check_ignores_same_ticker_and_dedupes() -> None:
    c = {corr_key("NVDA", "AMD"): 0.9}
    chk = cluster_check("NVDA", ["NVDA", "AMD", "amd"], c, threshold=0.7, max_names=2)
    assert chk.peers == ("AMD",) and not chk.blocked
    assert chk.describe() == "AMD"


def test_cluster_check_empty_book() -> None:
    chk = cluster_check("NVDA", [], {}, threshold=0.7, max_names=1)
    assert chk.peers == () and not chk.blocked and chk.describe() == "none"


# ---------------------------------------------------------------------------
# rule cluster_cap
# ---------------------------------------------------------------------------


def test_shipped_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # the suite pins the rule off (tests/pre_d85_env.py)
    monkeypatch.delenv("ARC_CLUSTER_CAP_ENABLED", raising=False)
    s = ArcSettings(_env_file=None)  # type: ignore[call-arg]
    assert s.cluster_cap_enabled is True
    assert s.cluster_cap_corr_threshold == 0.7
    assert s.cluster_cap_max_names == 2


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("cluster_cap_corr_threshold", 0.49),
        ("cluster_cap_corr_threshold", 0.96),
        ("cluster_cap_max_names", 0),
        ("cluster_cap_max_names", 11),
    ],
)
def test_setting_bounds(field: str, bad: float) -> None:
    with pytest.raises(ValueError, match=field):
        cfg(**{field: bad})


def test_two_correlated_names_block_the_third() -> None:
    [v] = check(book("QQQ", "IWM"), corrs(QQQ=0.9, IWM=0.8))
    assert v.code == RuleCode.CLUSTER_CAP
    assert v.detail == (
        "SPY would be name 3 of its correlation cluster (corr > 0.7; max 2 open names): IWM, QQQ"
    )


def test_one_correlated_name_passes() -> None:
    assert check(book("QQQ", "KO"), corrs(QQQ=0.9, KO=0.1)) == []


def test_threshold_is_strict() -> None:
    assert check(book("QQQ", "IWM"), corrs(QQQ=0.7, IWM=0.9)) == []  # 0.7 is not > 0.7
    assert check(book("QQQ", "IWM"), corrs(QQQ=0.7000001, IWM=0.9))


def test_threshold_setting_moves_the_edge() -> None:
    c = corrs(QQQ=0.8, IWM=0.8)
    assert check(book("QQQ", "IWM"), c)
    assert check(book("QQQ", "IWM"), c, cluster_cap_corr_threshold=0.85) == []


def test_negative_correlation_is_not_a_cluster() -> None:
    assert check(book("QQQ", "IWM"), corrs(QQQ=-0.95, IWM=0.9)) == []


def test_missing_pair_counts_and_is_named() -> None:
    [v] = check(book("QQQ", "IWM"), corrs(QQQ=0.9))
    assert v.detail.endswith("IWM, QQQ (no correlation stored for IWM: counted)")
    [v] = check(book("QQQ", "IWM"), {})  # rule on with no data: every pair counts
    assert "no correlation stored for IWM, QQQ" in v.detail


def test_reserved_in_flight_opens_count() -> None:
    c = corrs(QQQ=0.9, IWM=0.9)
    assert check(book("QQQ"), c) == []
    assert check(book("QQQ", reserved=("IWM",)), c)
    assert check(book(reserved=("QQQ", "IWM")), c)


def test_same_ticker_positions_are_not_peers() -> None:
    c = corrs(QQQ=0.9)
    assert check(book("SPY", "SPY", "QQQ", reserved=("SPY",)), c) == []


def test_two_structures_on_one_peer_count_once() -> None:
    assert check(book("QQQ", "QQQ"), corrs(QQQ=0.9)) == []


@pytest.mark.parametrize(("max_names", "fails"), [(1, True), (2, True), (3, False)])
def test_max_names_setting(max_names: int, fails: bool) -> None:
    out = check(book("QQQ", "IWM"), corrs(QQQ=0.9, IWM=0.9), cluster_cap_max_names=max_names)
    assert bool(out) is fails


def test_off_never_fails() -> None:
    assert check(book("QQQ", "IWM"), {}, cluster_cap_enabled=False) == []


@given(
    n_corr=st.integers(min_value=0, max_value=6),
    n_low=st.integers(min_value=0, max_value=6),
    n_missing=st.integers(min_value=0, max_value=6),
    max_names=st.integers(min_value=1, max_value=10),
)
def test_rule_fails_iff_peers_reach_max(
    n_corr: int, n_low: int, n_missing: int, max_names: int
) -> None:
    hi = [f"H{i}" for i in range(n_corr)]
    lo = [f"L{i}" for i in range(n_low)]
    miss = [f"M{i}" for i in range(n_missing)]
    c = {corr_key("SPY", t): 0.9 for t in hi} | {corr_key("SPY", t): 0.3 for t in lo}
    out = check(book(*hi, *lo, *miss), c, cluster_cap_max_names=max_names)
    assert bool(out) == (n_corr + n_missing >= max_names)


# ---------------------------------------------------------------------------
# evaluate()
# ---------------------------------------------------------------------------


def test_evaluate_runs_the_rule_for_opens() -> None:
    blocked = run(portfolio=book("QQQ", "IWM"), config=cfg(), market=mkt(correlations={}))
    assert any(v.startswith("cluster_cap:") for v in blocked.violations)
    ok = run(
        portfolio=book("QQQ", "IWM"),
        config=cfg(),
        market=mkt(correlations=corrs(QQQ=0.1, IWM=0.1)),
    )
    assert not any(v.startswith("cluster_cap:") for v in ok.violations)


def test_evaluate_exempts_closes() -> None:
    from arc.gate import evaluate

    held = Portfolio(
        legs={SP: -2, LP: 2},
        positions=[Position(underlying=n, max_loss=D("100")) for n in ("QQQ", "IWM", "SPY")],
    )
    close = evaluate(make_proposal(), acct(), held, cfg(), market=mkt(), now=NOW, closing=True)
    assert not any(v.startswith("cluster_cap:") for v in close.violations)
    opening = evaluate(make_proposal(), acct(), held, cfg(), market=mkt(), now=NOW)
    assert any(v.startswith("cluster_cap:") for v in opening.violations)


def test_off_is_identical_to_a_gate_without_the_rule() -> None:
    """Rule off: the decision equals the one for a book with no other names at all."""
    off = run(portfolio=book("QQQ", "IWM"), config=cfg(cluster_cap_enabled=False))
    empty = run(portfolio=book(), config=cfg(cluster_cap_enabled=False))
    assert off.violations == empty.violations
    assert off.passed == empty.passed


def test_snapshot_default_has_no_correlations() -> None:
    assert MarketSnapshot().correlations == {}


def test_reserved_underlyings_not_dumped() -> None:
    pf = book("QQQ", reserved=("IWM",))
    assert "reserved_underlyings" not in pf.model_dump()
    assert "reserved_underlyings" not in pf.model_dump_json()
