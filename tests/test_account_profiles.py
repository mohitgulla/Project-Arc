"""E3.4 / D25: account profiles, the ``account_profile`` gate rule, the whitelist split,
debit scanner strategies and profile-driven pipeline routing.

Gate tests here also run under ``make test-gate`` (100% branch coverage of
``arc.gate``).
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D
from pathlib import Path

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from arc.account_profiles import (
    DEFAULT_ACCOUNT_PROFILE,
    STRATEGY_NAMES,
    AccountProfiles,
    BuyingPower,
    ShortLegPolicy,
    load_account_profiles,
)
from arc.config import ArcSettings
from arc.gate import AccountSnapshot, MarketSnapshot, Portfolio, Quote, RuleCode, evaluate
from arc.gate import rules as R
from arc.models import Leg, LegIntent, Proposal, QuantMetrics, Sizing, Structure
from arc.models import StructureKind as MK
from arc.structures import (
    MarketInputs,
    credit_vertical,
    debit_vertical,
    format_occ,
    iron_condor,
    long_call,
    long_put,
)
from arc.utils.calendar import ET

EXP = dt.date(2026, 11, 20)
AS_OF = dt.date(2026, 10, 9)
NOW = dt.datetime(2026, 10, 9, 10, 0, tzinfo=ET)  # 42 DTE: inside 30-45 and 30-60


def cfg(profile: str = "cash_debit", **kw: object) -> ArcSettings:
    return ArcSettings(_env_file=None, account_profile=profile, **kw)  # type: ignore[call-arg]


def occ(kind: str, k: float) -> str:
    return format_occ("SPY", EXP, kind, k)


# Re-pricing always carries IV on every leg (E5.2a), so fixtures have real Greeks
# and the gate's missing_greeks rule does not mask the account-profile checks.
MARKET = MarketInputs(
    spot=575.0,
    r=0.04,
    ivs={occ(k, s): 0.2 for k in ("call", "put") for s in range(500, 655, 5)},
)


def bull_call() -> Structure:
    """Long 570C 6.00 / short 580C 2.00 -> debit 4.00, max loss $400, width $10."""
    return debit_vertical(
        "call",
        "SPY",
        EXP,
        long_strike=570,
        long_premium="6.00",
        short_strike=580,
        short_premium="2.00",
        as_of=AS_OF,
        market=MARKET,
    )


def bull_put() -> Structure:
    return credit_vertical(
        "put",
        "SPY",
        EXP,
        short_strike=570,
        short_premium="2.10",
        long_strike=565,
        long_premium="1.25",
        as_of=AS_OF,
        market=MARKET,
    )


def condor() -> Structure:
    return iron_condor(
        "SPY",
        EXP,
        long_put_strike=540,
        long_put_premium="0.50",
        short_put_strike=545,
        short_put_premium="1.00",
        short_call_strike=600,
        short_call_premium="1.00",
        long_call_strike=605,
        long_call_premium="0.50",
        as_of=AS_OF,
        market=MARKET,
    )


def quotes_for(s: Structure, half: str = "0.05") -> dict[str, Quote]:
    h = D(half)
    return {
        leg.occ_symbol: Quote(
            bid=leg.premium - h,  # type: ignore[operator]
            ask=leg.premium + h,  # type: ignore[operator]
            as_of=NOW - dt.timedelta(seconds=5),
        )
        for leg in s.legs
    }


def proposal(s: Structure, contracts: int = 2, **kw: object) -> Proposal:
    base: dict[str, object] = {
        "candidate_id": "c1",
        "structure": s,
        "thesis": "t",
        "quant": QuantMetrics(pop=0.5, ev=D("1"), cost_bps=1.0),
        "sizing": Sizing(contracts=contracts, notional=D("800"), pct_equity=0.008),
        "expires_at": NOW + dt.timedelta(minutes=10),
    }
    base.update(kw)
    return Proposal(**base)  # type: ignore[arg-type]


def acct(cash: str | None = "100000") -> AccountSnapshot:
    return AccountSnapshot(
        equity=D("100000"),
        last_equity=D("100000"),
        settled_cash=None if cash is None else D(cash),
        as_of=NOW - dt.timedelta(seconds=5),
    )


def gate(s: Structure, config: ArcSettings, account: AccountSnapshot | None = None, **kw: object):
    return evaluate(
        proposal(s, **kw),
        account or acct(),
        Portfolio(),
        config,
        market=MarketSnapshot(
            quotes=quotes_for(s), next_earnings={"SPY": None}, underlying_spot={"SPY": D("580")}
        ),
        now=NOW,
    )


def codes(decision) -> set[str]:  # noqa: ANN001
    return {v.split(":", 1)[0] for v in decision.violations}


PROFILE_CODES = {
    RuleCode.ACCOUNT_KIND,
    RuleCode.ACCOUNT_NET_DEBIT,
    RuleCode.ACCOUNT_SHORT_LEG,
    RuleCode.ACCOUNT_CASH,
}


# ---------------------------------------------------------------------------
# Profiles config
# ---------------------------------------------------------------------------


class TestProfilesConfig:
    def test_shipped_profiles(self) -> None:
        p = load_account_profiles()
        assert set(p.profiles) == {"margin", "cash_debit", "cash_long_only"}
        cd = p.get("cash_debit")
        assert set(cd.allowed_kinds) == {MK.VERTICAL_DEBIT, MK.LONG_CALL, MK.LONG_PUT}
        assert cd.require_net_debit and cd.buying_power is BuyingPower.CASH_SETTLED
        assert cd.allow_short_legs is ShortLegPolicy.COVERED_ONLY
        assert (cd.dte_min, cd.dte_max) == (30, 60)
        assert cd.strategies_for("bullish") == ["bull_call_debit", "long_call"]
        assert cd.strategies_for("bearish") == ["bear_put_debit", "long_put"]
        assert cd.strategies_for("neutral") == []
        lo = p.get("cash_long_only")
        assert lo.allow_short_legs is ShortLegPolicy.NONE
        assert set(lo.allowed_kinds) == {MK.LONG_CALL, MK.LONG_PUT}
        m = p.get("margin")
        assert m.allow_short_legs is ShortLegPolicy.ANY and not m.require_net_debit
        assert m.strategies_for("neutral") == ["iron_condor"]
        assert (m.dte_min, m.dte_max) == (None, None)
        assert "net debit only" in cd.summary() and "settled cash" in cd.summary()
        assert "no short legs" in lo.summary()
        assert "net debit" not in m.summary()

    def test_paper_default_is_cash_debit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ARC_ACCOUNT_PROFILE", raising=False)
        s = ArcSettings(_env_file=None)  # type: ignore[call-arg]
        assert DEFAULT_ACCOUNT_PROFILE == "cash_debit"
        assert s.account_profile == "cash_debit"
        assert s.profile.name == "cash_debit"
        assert s.entry_dte_window == (30, 60)

    def test_env_switch_needs_no_code_change(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARC_ACCOUNT_PROFILE", "margin")
        s = ArcSettings(_env_file=None)  # type: ignore[call-arg]
        assert s.profile.name == "margin"
        assert s.entry_dte_window == (s.dte_min, s.dte_max)

    def test_new_profile_from_yaml_only(self, tmp_path: Path) -> None:
        """A profile added in YAML is selectable and enforced with no code change."""
        data = yaml.safe_load(Path("config/account_profiles.yaml").read_text())
        data["profiles"]["calls_only"] = {
            "allowed_kinds": ["long_call"],
            "require_net_debit": True,
            "buying_power": "cash_settled",
            "allow_short_legs": "none",
            "dte_min": 20,
            "dte_max": 50,
            "stance_strategies": {"bullish": ["long_call"]},
        }
        f = tmp_path / "profiles.yaml"
        f.write_text(yaml.safe_dump(data))
        s = cfg("calls_only", account_profiles_file=f)
        assert s.entry_dte_window == (20, 50)
        assert s.profile.strategies_for("bearish") == []
        call = long_call("SPY", EXP, 570, "6.00", as_of=AS_OF)
        put = long_put("SPY", EXP, 570, "6.00", as_of=AS_OF)
        assert gate(call, s).passed
        assert RuleCode.ACCOUNT_KIND in codes(gate(put, s))

    def test_unknown_profile_fails_at_load(self) -> None:
        with pytest.raises(KeyError, match="unknown account profile 'nope'"):
            cfg("nope")

    def test_with_profile(self) -> None:
        s = cfg("margin").with_profile("cash_long_only")
        assert s.profile.name == "cash_long_only"
        with pytest.raises(KeyError):
            cfg().with_profile("nope")

    def test_unresolved_profile_raises(self) -> None:
        s = cfg().model_copy(update={"account_profile": "margin"})
        with pytest.raises(ValueError, match="not resolved"):
            _ = s.profile

    @pytest.mark.parametrize(
        ("body", "match"),
        [
            ({"allowed_kinds": ["other"]}, "never an allowed kind"),
            ({"dte_min": 30}, "both dte_min and dte_max"),
            ({"dte_min": 60, "dte_max": 30}, "dte_max 30 < dte_min 60"),
            ({"stance_strategies": {"bullish": ["short_strangle"]}}, "bullish"),
            ({"surprise": 1}, "surprise"),
        ],
    )
    def test_validation(self, body: dict[str, object], match: str) -> None:
        base: dict[str, object] = {
            "allowed_kinds": ["long_call"],
            "require_net_debit": True,
            "buying_power": "cash_settled",
            "allow_short_legs": "none",
            "stance_strategies": {},
        }
        with pytest.raises(ValueError, match=match):
            AccountProfiles.model_validate({"profiles": {"x": base | body}})

    def test_strategy_names_match_scanner(self) -> None:
        from arc.scanner import ScanStrategy

        assert {s.value for s in ScanStrategy} == STRATEGY_NAMES


# ---------------------------------------------------------------------------
# D4 kinds vs the account profile (E20.2: no separate whitelist setting)
# ---------------------------------------------------------------------------


class TestD4Kinds:
    def test_every_profile_kind_is_a_d4_kind(self) -> None:
        """The profile narrows D4; it can never name a kind outside it."""
        for prof in load_account_profiles().profiles.values():
            assert set(prof.allowed_kinds) <= R.D4_KINDS, prof.name
        assert set(load_account_profiles().get("margin").allowed_kinds) == R.D4_KINDS

    def test_profile_narrowing_is_enforced(self) -> None:
        """cash_debit refuses a credit vertical the margin profile accepts."""
        assert gate(bull_call(), cfg()).passed
        assert RuleCode.ACCOUNT_KIND in codes(gate(bull_put(), cfg()))


# ---------------------------------------------------------------------------
# Gate rule: account_profile
# ---------------------------------------------------------------------------


class TestGateAccountProfile:
    def test_debit_vertical_passes_under_cash_debit(self) -> None:
        d = gate(bull_call(), cfg())
        assert d.passed, d.violations

    def test_long_options_pass_under_cash_long_only(self) -> None:
        for s in (
            long_call("SPY", EXP, 570, "6.00", as_of=AS_OF),
            long_put("SPY", EXP, 570, "6.00", as_of=AS_OF),
        ):
            assert gate(s, cfg("cash_long_only")).passed

    def test_credit_condor_refused_under_cash_debit(self) -> None:
        d = gate(condor(), cfg())
        assert not d.passed
        got = codes(d)
        # a condor's wings cap the loss, but a cheaper long does not cover a short leg
        assert {
            RuleCode.ACCOUNT_KIND,
            RuleCode.ACCOUNT_NET_DEBIT,
            RuleCode.ACCOUNT_SHORT_LEG,
        } <= got
        assert any("iron_condor is not allowed under profile cash_debit" in v for v in d.violations)

    def test_credit_vertical_refused_under_cash_debit(self) -> None:
        got = codes(gate(bull_put(), cfg()))
        assert {RuleCode.ACCOUNT_KIND, RuleCode.ACCOUNT_NET_DEBIT} <= got

    def test_condor_passes_under_margin(self) -> None:
        d = gate(condor(), cfg("margin"))
        assert not (codes(d) & PROFILE_CODES), d.violations
        assert d.passed, d.violations

    def test_debit_vertical_refused_under_long_only(self) -> None:
        d = gate(bull_call(), cfg("cash_long_only"))
        got = codes(d)
        assert {RuleCode.ACCOUNT_KIND, RuleCode.ACCOUNT_SHORT_LEG} <= got
        assert any("allows no short legs" in v for v in d.violations)

    def test_uncovered_short_refused(self) -> None:
        """A 1x2 call ratio (long 570, short 2x 580) nets a debit but one short is naked."""
        legs = [
            Leg(occ_symbol=occ("c", 570), side=LegIntent.LONG, premium=D("6.00")),
            Leg(occ_symbol=occ("c", 580), side=LegIntent.SHORT, ratio=2, premium=D("2.00")),
        ]
        # direct rule call: the structure is not defined risk, so derive() would still work
        p = proposal(Structure(legs=legs, net_debit_credit=D("2.00"), dte=42))
        d = R.derive(p)
        v = R.check_account_profile(p, d, acct(), cfg())
        assert [x.code for x in v].count(RuleCode.ACCOUNT_SHORT_LEG) == 1
        assert occ("c", 580) in str(v)

    def test_wrong_side_long_does_not_cover(self) -> None:
        """Short 570C with long 580C (a credit call spread shape) is not covered."""
        legs = [
            Leg(occ_symbol=occ("c", 580), side=LegIntent.LONG, premium=D("2")),
            Leg(occ_symbol=occ("c", 570), side=LegIntent.SHORT, premium=D("6")),
        ]
        assert R._uncovered_shorts(legs) == [occ("c", 570)]
        # puts: long 570P covers short 560P; long 550P does not cover short 560P
        puts_ok = [
            Leg(occ_symbol=occ("p", 570), side=LegIntent.LONG, premium=D("6")),
            Leg(occ_symbol=occ("p", 560), side=LegIntent.SHORT, premium=D("2")),
        ]
        puts_bad = [
            Leg(occ_symbol=occ("p", 550), side=LegIntent.LONG, premium=D("1")),
            Leg(occ_symbol=occ("p", 560), side=LegIntent.SHORT, premium=D("2")),
        ]
        assert R._uncovered_shorts(puts_ok) == []
        assert R._uncovered_shorts(puts_bad) == [occ("p", 560)]
        # a long of another type or expiry never covers
        other_exp = format_occ("SPY", dt.date(2026, 12, 18), "c", 560)
        cross = [
            Leg(occ_symbol=occ("p", 560), side=LegIntent.LONG, premium=D("1")),
            Leg(occ_symbol=other_exp, side=LegIntent.LONG, premium=D("1")),
            Leg(occ_symbol=occ("c", 570), side=LegIntent.SHORT, premium=D("1")),
        ]
        assert R._uncovered_shorts(cross) == [occ("c", 570)]

    def test_covered_only_check_under_a_custom_profile(self, tmp_path: Path) -> None:
        """covered_only wired through evaluate(): an uncovered short fails the gate."""
        data = yaml.safe_load(Path("config/account_profiles.yaml").read_text())
        data["profiles"]["cover"] = data["profiles"]["cash_debit"] | {
            "allowed_kinds": ["vertical_credit", "vertical_debit"],
            "require_net_debit": False,
            "buying_power": "margin",
        }
        f = tmp_path / "p.yaml"
        f.write_text(yaml.safe_dump(data))
        s = cfg("cover", account_profiles_file=f)
        assert gate(bull_call(), s).passed
        # bull put: long 565P is worth less than short 570P below 570 -> not covered
        d = gate(bull_put(), s)
        assert codes(d) == {RuleCode.ACCOUNT_SHORT_LEG}, d.violations

    def test_settled_cash(self) -> None:
        # bull call 4.00 debit x 2 contracts = $800 + fees 0.05 x 2 legs x 2 = $0.20
        assert gate(bull_call(), cfg(), acct("800.20")).passed
        d = gate(bull_call(), cfg(), acct("800.19"))
        assert codes(d) == {RuleCode.ACCOUNT_CASH}
        assert "needs $800.20" in d.violations[0]
        unknown = gate(bull_call(), cfg(), acct(None))
        assert codes(unknown) == {RuleCode.ACCOUNT_CASH}
        assert "settled cash unknown" in unknown.violations[0]
        # margin profile: no cash check at all
        assert gate(bull_call(), cfg("margin"), acct("1")).passed

    def test_settled_cash_uses_band_worst_price(self) -> None:
        s = bull_call()
        p = proposal(s, limit_price=D("4.00"))
        m = MarketSnapshot(
            quotes=quotes_for(s), next_earnings={"SPY": None}, underlying_spot={"SPY": D("580")}
        )
        c = cfg()
        band = R.proposal_band(p, m, c)
        assert band.hi > D("4.00")
        worst = band.hi * 200 + D("0.20")
        ok = evaluate(p, acct(str(worst)), Portfolio(), c, market=m, now=NOW, band=band)
        assert RuleCode.ACCOUNT_CASH not in codes(ok)
        short = evaluate(
            p, acct(str(worst - D("0.01"))), Portfolio(), c, market=m, now=NOW, band=band
        )
        assert RuleCode.ACCOUNT_CASH in codes(short)

    def test_net_debit_required(self) -> None:
        """A debit-shaped structure with a credit limit fails the net-debit check."""
        s = bull_call()
        p = proposal(s)
        d = R.derive(p)._replace(limit_price=D("-0.10"))
        v = R.check_account_profile(p, d, acct(), cfg())
        assert [x.code for x in v] == [RuleCode.ACCOUNT_NET_DEBIT]

    def test_unresolved_profile_is_rule_error(self) -> None:
        broken = cfg().model_copy(update={"account_profile": "margin"})
        d = gate(bull_call(), broken)
        assert any(v.startswith("rule_error: account_profile: ValueError") for v in d.violations)

    def test_closing_skips_profile_rule(self) -> None:
        """Closing a credit spread under cash_debit is allowed (the rule limits opening only)."""
        s = bull_put()
        held = {leg.occ_symbol: (-1 if leg.side == LegIntent.LONG else 1) * 2 for leg in s.legs}
        # closing order = the opposite legs
        close = Structure(
            legs=[
                leg.model_copy(
                    update={
                        "side": LegIntent.SHORT if leg.side == LegIntent.LONG else LegIntent.LONG
                    }
                )
                for leg in s.legs
            ],
            net_debit_credit=-s.net_debit_credit,
            dte=s.dte,
        )
        d = evaluate(
            proposal(close),
            acct("0"),
            Portfolio(legs={k: -v for k, v in held.items()}),
            cfg(),
            market=MarketSnapshot(
                quotes=quotes_for(close),
                next_earnings={"SPY": None},
                underlying_spot={"SPY": D("580")},
            ),
            now=NOW,
            closing=True,
        )
        assert not (codes(d) & PROFILE_CODES), d.violations

    def test_profile_dte_window_reaches_gate(self) -> None:
        """cash_debit's 30-60 window (not the global 30-45) is what the gate enforces."""
        far = dt.date(2026, 12, 4)  # 56 DTE
        s = debit_vertical(
            "call", "SPY", far, long_strike=570, long_premium="7", short_strike=580,
            short_premium="3", as_of=AS_OF,
        )  # fmt: skip
        assert RuleCode.DTE_WINDOW not in codes(gate(s, cfg()))
        assert RuleCode.DTE_WINDOW in codes(gate(s, cfg("margin")))


@settings(max_examples=60, deadline=None)
@given(
    kind=st.sampled_from(["c", "p"]),
    strikes=st.lists(st.integers(50, 70), min_size=1, max_size=4),
    shorts=st.lists(st.integers(50, 70), min_size=0, max_size=4),
)
def test_uncovered_shorts_matches_bruteforce(
    kind: str, strikes: list[int], shorts: list[int]
) -> None:
    """Greedy coverage == exhaustive matching: a short is covered iff it can be matched to a
    distinct long that is at least as valuable at every price."""
    from itertools import permutations

    longs = [Leg(occ_symbol=occ(kind, k), side=LegIntent.LONG, premium=D(1)) for k in strikes]
    sh = [Leg(occ_symbol=occ(kind, k), side=LegIntent.SHORT, premium=D(1)) for k in shorts]
    got = len(R._uncovered_shorts([*longs, *sh]))

    def covers(lk: int, sk: int) -> bool:
        return lk <= sk if kind == "c" else lk >= sk

    # exhaustive: assign each short to a distinct long or to None (uncovered)
    slots = [*range(len(strikes)), *([None] * len(shorts))]
    best = 0
    for perm in set(permutations(slots, len(shorts))):
        best = max(
            best,
            sum(i is not None and covers(strikes[i], shorts[j]) for j, i in enumerate(perm)),
        )
    assert got == len(shorts) - best
