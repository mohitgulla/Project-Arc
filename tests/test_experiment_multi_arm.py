"""E15.1 (D69): multi-arm specs, shared-account runner config, arm cap, concurrent
experiments, and the arm account identity guard."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from arc.experiments.arms import (
    ArmAccountChangedError,
    account_fingerprint,
    arm_stores,
    read_identity,
)
from arc.experiments.config import ArmRunner, ExperimentDefaults, RunnerConfig
from arc.experiments.models import (
    ExperimentSpec,
    ExperimentStatus,
    StopReason,
    canonical_json,
    spec_hash,
)
from arc.experiments.overlay import fill_defaults
from arc.experiments.runner import (
    ACCOUNT_CHANGED,
    ArmAccount,
    ArmStartError,
    arms_tick,
    start_arms,
)
from arc.experiments.store import ExperimentStore
from arc.experiments.tape import running_experiment, running_experiments
from arc.monitoring.alerts import RecordingOpsNotifier
from arc.pipeline.env import FIXTURE_NOW
from arc.routines.handlers import JobResult
from arc.store.migrate import migrate
from arc.store.repos import HaltRepo
from arc.utils.calendar import ET

REPO = Path(__file__).resolve().parents[1]
LIVE = REPO / "config" / "experiments" / "live"
T0 = FIXTURE_NOW - dt.timedelta(hours=1)
OWNER = "local"
ACCT = "PA3EXP0001"
OTHER_ACCT = "PA3EXP9999"

# Hashes locked before D69 (spec v1): loading under v2 must reproduce every one.
V1_HASHES = {
    "xp10_trending_velocity.yaml": "9c159cb68a47d3b8574a66b21eda55d78eee6762a0478a4d664196d49f96e245",  # noqa: E501
    "xp11_scalp_movers.yaml": "b511804da283981b3622a0c166e245a19325a2ad7b80f258775c1af2610d6ee9",  # noqa: E501
    "xp12_retail_sentiment.yaml": "c88e20f231a67444653ba5d29c0e596ceadd7aea7336c6159faf5803c1b518fb",  # noqa: E501
    "xp1_aa_baseline.yaml": "7d02a85573156876e3b8d0a4e57e195d9ce04f6399966103843224d8749d8d20",  # noqa: E501
    "xp2_finnhub_context.yaml": "250081383bbbea670a666c2a6ee3e2934c81e752c864fbe17bc9541f1ae8665b",  # noqa: E501
    "xp3_relaxed_diversification.yaml": "e4a3731068aa835adee26df6265ad5a824a423304eb3d21703ee56634c281c74",  # noqa: E501
}


def _db(path: Path | str = ":memory:") -> sqlite3.Connection:
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    migrate(c)
    return c


def _spec(eid: str, *, k: int = 3, kind: str = "aa", area: str = "other") -> ExperimentSpec:
    overlay = {"exits": {"default": {"take_profit_pct": 0.4}}} if kind == "ab" else {}
    data: dict[str, Any] = {
        "spec_version": 2,
        "id": eid,
        "title": f"{eid} title",
        "hypothesis": "noise floor",
        "area": area,
        "kind": kind,
        "proposed_by": "owner",
        "arms": {"treatments": {f"t{i}": {"overlay": overlay} for i in range(1, k + 1)}},
    }
    if kind == "ab":
        data |= {"backtest_ref": "data/backtests/e75a/compare.json", "non_inferiority_margin": 0.1}
    return fill_defaults(ExperimentSpec.model_validate(data), ExperimentDefaults())


def _shared(**kw: Any) -> RunnerConfig:
    return RunnerConfig(
        account_mode="shared",
        arms={
            "t": ArmRunner(
                spec_arm="treatments", keys_env="ALPACA_EXP", db="exp-{experiment_id}-{arm}.db"
            )
        },
        **kw,
    )


def _dedicated() -> RunnerConfig:
    return RunnerConfig(
        arms={
            "treatment": ArmRunner(
                spec_arm="treatment", keys_env="ALPACA_EXP", db="exp-{experiment_id}.db"
            )
        }
    )


def _probe(number: str = ACCT, equity: str = "100000", positions: int = 0, orders: int = 0) -> Any:
    calls: list[str] = []

    def probe(keys_env: str) -> ArmAccount:
        calls.append(keys_env)
        return ArmAccount(
            account_number=number, equity=D(equity), positions=positions, open_orders=orders
        )

    probe.calls = calls  # type: ignore[attr-defined]
    return probe


def _registered(conn: sqlite3.Connection, spec: ExperimentSpec, runner: RunnerConfig) -> None:
    store = ExperimentStore.for_runner(conn, runner)
    store.create(spec, actor=OWNER)
    assert store.register(spec.id, actor=OWNER).status is ExperimentStatus.REGISTERED


def _start(
    conn: sqlite3.Connection,
    eid: str,
    runner: RunnerConfig,
    tmp_path: Path,
    *,
    probe: Any = None,
    t0: str = "10000",
) -> Any:
    return start_arms(
        conn,
        eid,
        actor=OWNER,
        now=T0,
        t0_equity=D(t0),
        runner=runner,
        arm_dir=tmp_path / "arms",
        control_sha="abcdef1",
        aa_override=True,
        probe=probe if probe is not None else _probe(),
    )


# --- spec v1 -> v2 ------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(V1_HASHES))
def test_every_committed_v1_spec_loads_as_t1_with_its_hash_unchanged(name: str) -> None:
    raw = yaml.safe_load((LIVE / name).read_text())
    assert "treatment" in raw["arms"]  # committed as v1
    sp = ExperimentSpec.model_validate(raw)
    assert sp.spec_version == 1 and sp.arms.names == ["t1"]
    assert sp.arms.arm("treatment") is sp.arms.treatments["t1"]
    assert spec_hash(sp) == V1_HASHES[name]
    # the hashed document is still the v1 document
    assert '"treatment":' in canonical_json(sp) and '"treatments"' not in canonical_json(sp)


# Specs committed as v2 (D69 multi-arm); never loaded as v1.
V2_SPECS = {
    "xp13_technicals.yaml",  # E16.3: draft, unregistered
    "xp14_menu_measure.yaml",  # E7.5b: draft, unregistered
}


def test_committed_specs_are_all_covered() -> None:
    assert {p.name for p in LIVE.glob("*.yaml")} == set(V1_HASHES) | V2_SPECS


@pytest.mark.parametrize("name", sorted(V2_SPECS))
def test_committed_v2_specs_load_with_their_arms(name: str) -> None:
    sp = ExperimentSpec.model_validate(yaml.safe_load((LIVE / name).read_text()))
    assert sp.spec_version == 2 and len(sp.arms.names) >= 2


def test_a_v1_spec_round_trips_through_its_stored_json() -> None:
    sp = ExperimentSpec.model_validate(
        yaml.safe_load((LIVE / "xp2_finnhub_context.yaml").read_text())
    )
    again = ExperimentSpec.model_validate(json.loads(canonical_json(sp)))
    assert spec_hash(again) == spec_hash(sp) and again.spec_version == 1


def test_k3_spec_round_trips_and_orders_treatments() -> None:
    sp = _spec("XP-20", k=3)
    assert sp.spec_version == 2 and sp.arms.names == ["t1", "t2", "t3"]
    doc = json.loads(canonical_json(sp))
    assert set(doc["arms"]) == {"control", "treatments"}
    again = ExperimentSpec.model_validate(doc)
    assert again == sp and spec_hash(again) == spec_hash(sp)
    # t10 sorts after t2
    data = sp.model_dump(mode="json")
    data["arms"]["treatments"] = {"t10": {"overlay": {}}, "t2": {"overlay": {}}}
    assert ExperimentSpec.model_validate(data).arms.names == ["t2", "t10"]
    with pytest.raises(ValueError, match="3 treatments"):
        _ = sp.arms.treatment
    with pytest.raises(KeyError):
        sp.arms.arm("t4")


@pytest.mark.parametrize(
    ("arms", "match"),
    [
        ({"treatments": {"treatment": {}}}, "reserved"),
        ({"treatments": {"control": {}}}, "reserved"),
        ({"treatments": {"t0": {}}}, "t1..t16"),
        ({"treatments": {"t17": {}}}, "t1..t16"),
        ({"treatments": {"T1": {}}}, "t1..t16"),
        ({"treatments": {}}, "at least one"),
        ({"treatment": {}, "treatments": {"t1": {}}}, "not both"),
    ],
)
def test_treatment_names_are_validated(arms: dict[str, Any], match: str) -> None:
    data = {"spec_version": 2, "id": "XP-21", "title": "t", "hypothesis": "h", "area": "other",
            "kind": "aa", "proposed_by": "owner", "arms": arms}  # fmt: skip
    with pytest.raises(ValidationError, match=match):
        ExperimentSpec.model_validate(data)


def test_aa_needs_every_overlay_empty_and_ab_every_overlay_set() -> None:
    base = {"spec_version": 2, "id": "XP-22", "title": "t", "hypothesis": "h", "area": "exits",
            "proposed_by": "owner"}  # fmt: skip
    ov = {"overlay": {"exits": {"default": {"take_profit_pct": 0.4}}}}
    with pytest.raises(ValidationError, match="t2 has one"):
        ExperimentSpec.model_validate(
            base | {"kind": "aa", "arms": {"treatments": {"t1": {}, "t2": ov}}}
        )
    with pytest.raises(ValidationError, match="overlay .what changes. on t2"):
        ExperimentSpec.model_validate(
            base
            | {
                "kind": "ab",
                "backtest_ref": "x",
                "non_inferiority_margin": 0.1,
                "arms": {"treatments": {"t1": ov, "t2": {}}},
            }  # fmt: skip
        )


def test_v1_version_with_several_treatments_is_refused() -> None:
    data = {"spec_version": 1, "id": "XP-23", "title": "t", "hypothesis": "h", "area": "other",
            "kind": "aa", "proposed_by": "owner",
            "arms": {"treatments": {"t1": {}, "t2": {}}}}  # fmt: skip
    with pytest.raises(ValidationError, match="spec_version 2"):
        ExperimentSpec.model_validate(data)


# --- runner config --------------------------------------------------------------------


def test_shared_mode_validator() -> None:
    # dedicated (default): two arms may not share keys; a template is shared-only
    with pytest.raises(ValidationError, match="share broker keys"):
        RunnerConfig(
            arms={
                "a": ArmRunner(spec_arm="t1", keys_env="ALPACA_EXP", db="a-{experiment_id}.db"),
                "b": ArmRunner(spec_arm="t2", keys_env="ALPACA_EXP", db="b-{experiment_id}.db"),
            }
        )
    with pytest.raises(ValidationError, match="only account_mode: shared"):
        RunnerConfig(arms=_shared().arms)
    # shared: keys may repeat
    cfg = RunnerConfig(
        account_mode="shared",
        arms={
            "a": ArmRunner(spec_arm="t1", keys_env="ALPACA_EXP", db="a-{experiment_id}.db"),
            "b": ArmRunner(spec_arm="t2", keys_env="ALPACA_EXP", db="b-{experiment_id}.db"),
        },
    )
    assert cfg.account_mode == "shared"
    with pytest.raises(ValidationError, match="needs '\\{arm\\}'"):
        ArmRunner(spec_arm="treatments", keys_env="ALPACA_EXP", db="exp-{experiment_id}.db")
    with pytest.raises(ValidationError, match="production/test keys"):
        ArmRunner(spec_arm="treatments", keys_env="ALPACA", db="exp-{experiment_id}-{arm}.db")
    with pytest.raises(ValidationError, match="spec_arm"):
        ArmRunner(spec_arm="bogus", keys_env="ALPACA_EXP", db="x.db")
    with pytest.raises(ValidationError):
        RunnerConfig(max_parallel_arms=17)
    with pytest.raises(ValidationError):
        RunnerConfig(account_mode="pooled")  # type: ignore[arg-type]


def test_template_expands_one_arm_per_treatment() -> None:
    arms = _shared().arms_for(_spec("XP-20", k=3))
    assert list(arms) == ["t1", "t2", "t3"]
    assert [a.spec_arm for a in arms.values()] == ["t1", "t2", "t3"]
    assert arms["t2"].db_path("XP-20") == "exp-XP-20-t2.db"
    assert {a.keys_env for a in arms.values()} == {"ALPACA_EXP"}
    # a K=3 spec on the one-arm dedicated config: t2, t3 would never run
    with pytest.raises(ValueError, match="t2, t3 have no runner arm"):
        _dedicated().arms_for(_spec("XP-20", k=3))
    with pytest.raises(ValueError, match="no arm 't4'"):
        RunnerConfig(
            arms={"x": ArmRunner(spec_arm="t4", keys_env="ALPACA_EXP", db="x-{experiment_id}.db")}
        ).arms_for(_spec("XP-20", k=3))


def test_shipped_experiments_yaml_is_dedicated_with_new_defaults() -> None:
    from arc.experiments.config import load_experiments_config

    r = load_experiments_config().runner
    assert r.account_mode == "dedicated"
    assert r.max_parallel_arms == 10 and r.shared_capacity_frac == 0.9
    assert r.enabled is True and list(r.arms) == ["treatment"]


def test_new_runner_knobs_are_registered_with_ceilings() -> None:
    from arc.control.registry import REGISTRY, TunableError, lookup

    t = REGISTRY["experiments.runner.max_parallel_arms"]
    assert t.max == 16 and t.hard_ceiling == 16 and t.min == 1
    f = REGISTRY["experiments.runner.shared_capacity_frac"]
    assert f.hard_ceiling == 1.0
    # account_mode is topology: never settable
    assert "experiments.runner.account_mode" not in REGISTRY
    with pytest.raises(TunableError):
        lookup("experiments.runner.account_mode")


def test_max_parallel_arms_override_reaches_the_runner(tmp_path: Path) -> None:
    from arc.config import ArcSettings
    from arc.control.service import ControlService
    from arc.experiments.runner import runner_config

    conn = _db(tmp_path / "c.db")
    svc = ControlService(conn, base=ArcSettings(), now=lambda: T0, is_halted=lambda: False)
    assert (
        svc.set("experiments.runner.max_parallel_arms", "4", actor=OWNER, source="cli").outcome
        == "applied"
    )
    assert runner_config(conn).max_parallel_arms == 4
    r = svc.set("experiments.runner.max_parallel_arms", "16", actor=OWNER, source="cli")
    assert r.pending is not None  # more arms is riskier: needs a confirm
    assert (
        svc.set("experiments.runner.max_parallel_arms", "17", actor=OWNER, source="cli").outcome
        == "refused"
    )


# --- registry: arm cap + A/A area exemption -----------------------------------------------


def test_max_parallel_arms_queues_then_promotes(tmp_path: Path) -> None:
    conn = _db()
    store = ExperimentStore.for_runner(conn, _shared(max_parallel_arms=5), now=lambda: T0)
    for eid, k in (("XP-20", 3), ("XP-21", 3), ("XP-22", 2)):
        store.create(_spec(eid, k=k), actor=OWNER)
    assert store.register("XP-20", actor=OWNER).status is ExperimentStatus.REGISTERED
    st = store.register("XP-21", actor=OWNER)  # 3 + 3 > 5
    assert st.status is ExperimentStatus.QUEUED
    assert st.events[-1].detail is not None
    assert "max_parallel_arms 5" in st.events[-1].detail["arm_cap"]
    assert store.register("XP-22", actor=OWNER).status is ExperimentStatus.REGISTERED  # 3 + 2
    store.stop("XP-20", StopReason.OWNER, actor=OWNER)  # 2 used: XP-21 (3) fits
    assert store.require("XP-21").status is ExperimentStatus.REGISTERED
    # one experiment wider than the cap could never start: refused outright
    store.create(_spec("XP-23", k=6), actor=OWNER)
    with pytest.raises(Exception, match="could never start"):  # noqa: PT011
        store.register("XP-23", actor=OWNER)


def test_aa_is_exempt_from_the_area_lock_but_ab_is_not(tmp_path: Path) -> None:
    store = ExperimentStore(_db(), now=lambda: T0)
    store.create(_spec("XP-20", kind="ab", area="exits", k=1), actor=OWNER)
    store.create(_spec("XP-21", kind="aa", area="exits", k=2), actor=OWNER)
    store.create(_spec("XP-22", kind="ab", area="exits", k=1), actor=OWNER)
    assert store.register("XP-20", actor=OWNER).status is ExperimentStatus.REGISTERED
    assert store.register("XP-21", actor=OWNER).status is ExperimentStatus.REGISTERED  # A/A
    assert store.register("XP-22", actor=OWNER).status is ExperimentStatus.QUEUED  # A/B
    assert [s.experiment_id for s in store.active_in_area("exits")] == ["XP-20"]


# --- t0 in shared mode ------------------------------------------------------------------------


def _control(tmp_path: Path) -> sqlite3.Connection:
    return _db(tmp_path / "control.db")


def test_k3_aa_starts_three_arm_stores_on_one_shared_account(tmp_path: Path) -> None:
    conn = _control(tmp_path)
    runner = _shared()
    _registered(conn, _spec("XP-20"), runner)
    probe = _probe()
    st = _start(conn, "XP-20", runner, tmp_path, probe=probe)
    assert st.status is ExperimentStatus.RUNNING
    assert probe.calls == ["ALPACA_EXP"]  # one read of the shared account
    stores = arm_stores(conn, "XP-20")
    assert list(stores) == ["t1", "t2", "t3"]
    assert {p.name for p in stores.values()} == {
        "exp-XP-20-t1.db",
        "exp-XP-20-t2.db",
        "exp-XP-20-t3.db",
    }
    sha, last4 = account_fingerprint(ACCT)
    for name, path in stores.items():
        arm = _db(path)
        ident = read_identity(arm)
        assert ident is not None
        assert ident.arm_id == f"XP-20:{name}" and ident.spec_arm == name
        assert ident.account_mode == "shared"
        assert ident.account_sha256 == sha and ident.account_last4 == last4
        assert ACCT not in json.dumps(ident.model_dump(mode="json"))  # never the raw number
        row = arm.execute("SELECT kind, amount FROM virtual_ledger").fetchall()
        assert [(r["kind"], r["amount"]) for r in row] == [("open", "10000")]
        arm.close()
    assert set(st.running.arm_plans) == {"t1", "t2", "t3"}  # type: ignore[union-attr]


def test_shared_capacity_refusal_names_the_shortfall(tmp_path: Path) -> None:
    conn = _control(tmp_path)
    runner = _shared()
    _registered(conn, _spec("XP-20"), runner)
    # 3 x 10k = 30k > 30k x 0.9 = 27k
    with pytest.raises(
        ArmStartError,
        match=r"too small.*\$30,000.00 > equity \$30,000.00 x 0.9 = \$27,000.00 \(short \$3,000.00\)",  # noqa: E501
    ):
        _start(conn, "XP-20", runner, tmp_path, probe=_probe(equity="30000"))
    assert arm_stores(conn) == {}
    assert not (tmp_path / "arms").exists() or not any((tmp_path / "arms").iterdir())
    assert ExperimentStore(conn).require("XP-20").status is ExperimentStatus.REGISTERED
    # running arms count too: XP-20 at 3 x 10k, then XP-21 (2 x 10k) on a 50k account
    _start(conn, "XP-20", runner, tmp_path, probe=_probe(equity="50000"))
    _registered(conn, _spec("XP-21", k=2), runner)
    with pytest.raises(ArmStartError, match=r"3 running arm\(s\) \$30,000.00 \+ 2 new"):
        _start(conn, "XP-21", runner, tmp_path, probe=_probe(equity="50000", positions=2))
    _start(conn, "XP-21", runner, tmp_path, probe=_probe(equity="60000", positions=2))


def test_first_arm_needs_a_flat_shared_account_later_ones_join_busy(tmp_path: Path) -> None:
    conn = _control(tmp_path)
    runner = _shared()
    _registered(conn, _spec("XP-20", k=2), runner)
    with pytest.raises(ArmStartError, match="1 position.*first arm on a shared"):
        _start(conn, "XP-20", runner, tmp_path, probe=_probe(positions=1))
    _start(conn, "XP-20", runner, tmp_path)
    _registered(conn, _spec("XP-21", k=2), runner)
    # XP-20's arms now hold positions on the account: XP-21 still joins
    st = _start(conn, "XP-21", runner, tmp_path, probe=_probe(positions=4, orders=1))
    assert st.status is ExperimentStatus.RUNNING
    assert set(arm_stores(conn)) == {"XP-20:t1", "XP-20:t2", "XP-21:t1", "XP-21:t2"}


def test_start_refuses_over_max_parallel_arms(tmp_path: Path) -> None:
    conn = _control(tmp_path)
    big = _shared(max_parallel_arms=16)
    _registered(conn, _spec("XP-20", k=3), big)
    _registered(conn, _spec("XP-21", k=3), big)
    _start(conn, "XP-20", big, tmp_path, t0="1000")
    with pytest.raises(ArmStartError, match="max_parallel_arms 4: 3 arm"):
        _start(conn, "XP-21", _shared(max_parallel_arms=4), tmp_path, t0="1000")


def test_dedicated_and_shared_arms_never_share_an_account(tmp_path: Path) -> None:
    conn = _control(tmp_path)
    one = _spec("XP-20", k=1)
    _registered(conn, one, _dedicated())
    _start(conn, "XP-20", _dedicated(), tmp_path)
    _registered(conn, _spec("XP-21", k=2), _shared())
    with pytest.raises(ArmStartError, match="XP-20.*dedicated mode.*stop XP-20 first"):
        _start(conn, "XP-21", _shared(), tmp_path)
    # the other direction: a dedicated arm onto a shared account
    conn2 = _db(tmp_path / "control2.db")
    _registered(conn2, _spec("XP-21", k=2), _shared())
    _start(conn2, "XP-21", _shared(), tmp_path / "b")
    _registered(conn2, one, _dedicated())
    with pytest.raises(ArmStartError, match="XP-21.*shared mode"):
        _start(conn2, "XP-20", _dedicated(), tmp_path / "b")
    # two dedicated experiments never share one account either
    _registered(conn, _spec("XP-22", k=1, area="exits"), _dedicated())
    with pytest.raises(ArmStartError, match="dedicated arm account already used by XP-20"):
        _start(conn, "XP-22", _dedicated(), tmp_path)
    # once XP-20 stops, XP-21 starts in shared mode on the same keys
    ExperimentStore(conn).stop("XP-20", StopReason.OWNER, actor=OWNER)
    assert _start(conn, "XP-21", _shared(), tmp_path).status is ExperimentStatus.RUNNING


def test_start_refuses_while_an_experiment_runs_on_the_old_account(tmp_path: Path) -> None:
    conn = _control(tmp_path)
    runner = _shared()
    _registered(conn, _spec("XP-20", k=2), runner)
    _start(conn, "XP-20", runner, tmp_path)
    _registered(conn, _spec("XP-21", k=2), runner)
    new = _probe(number=OTHER_ACCT, equity="1000000")
    with pytest.raises(
        ArmStartError, match=r"…9999, but running experiment\(s\) XP-20 .*stop them first"
    ):
        _start(conn, "XP-21", runner, tmp_path, probe=new)
    ExperimentStore(conn).stop("XP-20", StopReason.OWNER, actor=OWNER)
    assert _start(conn, "XP-21", runner, tmp_path, probe=new).status is ExperimentStatus.RUNNING


def test_two_experiments_never_share_an_arm_store_path(tmp_path: Path) -> None:
    conn = _control(tmp_path)
    fixed = RunnerConfig(
        account_mode="shared",
        arms={"t1": ArmRunner(spec_arm="t1", keys_env="ALPACA_EXP", db="exp-shared.db")},
    )
    _registered(conn, _spec("XP-20", k=1), fixed)
    _registered(conn, _spec("XP-21", k=1), fixed)
    _start(conn, "XP-20", fixed, tmp_path)
    with pytest.raises(ArmStartError, match="another arm's store"):
        _start(conn, "XP-21", fixed, tmp_path)


# --- concurrent experiments in the arms tick + the account guard ------------------------


def _stub_handlers(ran: list[str]) -> dict[str, Any]:
    def handler(name: str) -> Any:
        def run(ctx: Any) -> JobResult:
            ran.append(f"{Path(ctx.run_env.db_path).name}:{name}")
            return JobResult(summary=f"{name} stub")

        return run

    return {j: handler(j) for j in ("monitor", "positions.evaluate", "broker.reconcile", "broker")}


def test_running_experiments_lists_every_running_experiment(tmp_path: Path) -> None:
    conn = _control(tmp_path)
    runner = _shared()
    assert running_experiments(conn) == [] and running_experiment(conn) is None
    for eid in ("XP-20", "XP-21"):
        _registered(conn, _spec(eid, k=2), runner)
        _start(conn, eid, runner, tmp_path)
    assert [s.experiment_id for s in running_experiments(conn)] == ["XP-20", "XP-21"]
    assert running_experiment(conn).experiment_id == "XP-20"  # type: ignore[union-attr]


def test_two_running_experiments_both_get_arms_ticks(tmp_path: Path) -> None:
    conn = _control(tmp_path)
    runner = _shared()
    for eid in ("XP-20", "XP-21"):
        _registered(conn, _spec(eid, k=2), runner)
        _start(conn, eid, runner, tmp_path)
    ran: list[str] = []
    at = dt.datetime(2026, 10, 7, 10, 5, tzinfo=ET)
    report = arms_tick(
        conn,
        routines_path=None,
        now=at,
        lock_dir=None,
        handlers=_stub_handlers(ran),
        account_number=lambda keys: ACCT,
    )
    assert report["experiment_ids"] == ["XP-20", "XP-21"]
    assert set(report["arms"]) == {"XP-20:t1", "XP-20:t2", "XP-21:t1", "XP-21:t2"}
    assert all("error" not in a for a in report["arms"].values()), report["arms"]
    stores = {r.split(":")[0] for r in ran}
    assert stores == {"exp-XP-20-t1.db", "exp-XP-20-t2.db", "exp-XP-21-t1.db", "exp-XP-21-t2.db"}


def test_swapped_keys_halt_the_arm_and_send_no_order(tmp_path: Path) -> None:
    conn = _control(tmp_path)
    runner = _shared()
    _registered(conn, _spec("XP-20", k=2), runner)
    _start(conn, "XP-20", runner, tmp_path)
    ran: list[str] = []
    ops = RecordingOpsNotifier()
    at = dt.datetime(2026, 10, 7, 10, 5, tzinfo=ET)
    reads: list[str] = []

    def swapped(keys: str) -> str:
        reads.append(keys)
        return OTHER_ACCT

    for _ in range(2):  # the second tick neither re-halts nor re-posts
        report = arms_tick(
            conn, routines_path=None, now=at, lock_dir=None,
            handlers=_stub_handlers(ran), account_number=swapped, notifier=ops,
        )  # fmt: skip
        for arm in report["arms"].values():
            assert arm["error"].startswith(ACCOUNT_CHANGED)
            assert "jobs" not in arm and "pairs" not in arm
    assert ran == []  # nothing ran: no broker job, no order
    assert reads == ["ALPACA_EXP", "ALPACA_EXP"]  # one read per keys per tick
    assert len(ops.posts) == 2  # one notice per arm
    assert all("[Ops] Experiment XP-20" in p and ACCOUNT_CHANGED in p for p in ops.posts)
    for path in arm_stores(conn, "XP-20").values():
        arm = _db(path)
        halts = HaltRepo(arm).active()
        assert len(halts) == 1 and halts[0]["reason"].startswith(ACCOUNT_CHANGED)
        assert "…9999" in halts[0]["reason"] and OTHER_ACCT not in halts[0]["reason"]
        arm.close()


def test_an_arm_broker_on_swapped_keys_refuses_to_build(tmp_path: Path) -> None:
    from arc.experiments.broker import trading_broker

    conn = _control(tmp_path)
    runner = _shared()
    _registered(conn, _spec("XP-20", k=1), runner)
    _start(conn, "XP-20", runner, tmp_path)
    arm = _db(arm_stores(conn, "XP-20")["t1"])

    class _Broker:
        def __init__(self, number: str) -> None:
            self.number = number
            self.orders: list[Any] = []

        def account(self) -> Any:
            from arc.broker.base import AccountInfo

            return AccountInfo(
                account_id=self.number, equity=D(100000), buying_power=D(0), cash=D(0)
            )

        def submit_mleg(self, *a: Any, **k: Any) -> None:
            self.orders.append((a, k))

    env = {"ALPACA_EXP_API_KEY": "k", "ALPACA_EXP_SECRET_KEY": "s"}
    bad = _Broker(OTHER_ACCT)
    with pytest.raises(ArmAccountChangedError, match="…9999.*stop XP-20"):
        trading_broker(arm, factory=lambda k, s: bad, environ=env)  # type: ignore[arg-type,return-value]
    assert bad.orders == []
    good = _Broker(ACCT)
    trading_broker(arm, factory=lambda k, s: good, environ=env, now=lambda: T0)  # type: ignore[arg-type,return-value]


# --- fixtures CLI: create -> register -> start a K=3 A/A in shared mode ------------------------


def _arc(*argv: str) -> int:
    from arc.cli import main

    return main(list(argv))


def test_cli_fixture_k3_aa_shared(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    spec = tmp_path / "xp20.yaml"
    spec.write_text(yaml.safe_dump(json.loads(canonical_json(_spec("XP-20")))))
    cfg = yaml.safe_load((REPO / "config" / "experiments.yaml").read_text())
    cfg["experiments"]["runner"]["account_mode"] = "shared"
    cfg["experiments"]["runner"]["arms"] = {
        "t": {
            "spec_arm": "treatments",
            "keys_env": "ALPACA_EXP",
            "db": "data/arc-exp-{experiment_id}-{arm}.db",
        }  # fmt: skip
    }
    xcfg = tmp_path / "experiments.yaml"
    xcfg.write_text(yaml.safe_dump(cfg))
    db = ("--db", str(tmp_path / "arc.db"))
    assert _arc("experiment", "create", "--spec", str(spec), *db) == 0
    assert _arc("experiment", "register", "XP-20", *db) == 0
    capsys.readouterr()
    assert (
        _arc("experiment", "start", "XP-20", "--fixtures", "--t0-equity", "10000",
             "--arm-dir", str(tmp_path / "arms"), "--experiments-config", str(xcfg),
             "--now", T0.isoformat(), "--json", *db)
        == 0
    )  # fmt: skip
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("{\n") :])
    assert payload["status"] == "running"
    assert list(payload["arms"]) == ["t1", "t2", "t3"]
    for name, p in payload["arms"].items():
        assert Path(p).name == f"arc-exp-XP-20-{name}.db" and Path(p).is_file()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__]))
