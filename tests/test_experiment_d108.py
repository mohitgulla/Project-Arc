"""E25.1 (D108): spec v3, the flat control arm on the experiment account, the production
start book, and the control arm's fork rule. v1/v2 specs keep their meaning and hash."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from arc.experiments.arms import arm_stores, read_identity
from arc.experiments.config import (
    CONTROL_RUNNER,
    ArmRunner,
    ExperimentDefaults,
    RunnerConfig,
    load_experiments_config,
)
from arc.experiments.models import (
    CONTROL_ARM,
    PRODUCTION_ARM,
    SPEC_VERSION,
    ExperimentSpec,
    ExperimentStatus,
    RunningDetail,
    arm_id,
    canonical_json,
    spec_hash,
)
from arc.experiments.overlay import fill_defaults, load_spec
from arc.experiments.runner import ArmAccount, ArmStartError, control_fork, start_arms
from arc.experiments.store import ExperimentStore
from arc.pipeline.env import FIXTURE_NOW
from arc.store.execution import OpenStructureRepo
from arc.store.migrate import migrate
from arc.store.repos import CandidateRepo, ProposalRepo
from arc.structures import credit_vertical
from arc.utils.calendar import ET

REPO = Path(__file__).resolve().parents[1]
XP1 = REPO / "config" / "experiments" / "live" / "xp1_aa_baseline.yaml"
XP1_HASH = "7d02a85573156876e3b8d0a4e57e195d9ce04f6399966103843224d8749d8d20"
T0 = FIXTURE_NOW - dt.timedelta(hours=1)
ACCT = "PA3EXP0001"
EXITS = {"exits": {"default": {"take_profit_pct": 0.4}}}
UNIVERSE = {"universe": {"liquidity_screen": {"loose": {"min_price": 4.0}}}}


def _db(path: Path | str = ":memory:") -> sqlite3.Connection:
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    migrate(c)
    return c


def _doc(eid: str = "XP-30", *, version: int | None = 3, k: int = 3, **kw: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": eid,
        "title": f"{eid} title",
        "hypothesis": "noise floor",
        "area": "other",
        "kind": "aa",
        "proposed_by": "owner",
        "arms": {"treatments": {f"t{i}": {"overlay": {}} for i in range(1, k + 1)}},
    }
    if version is not None:
        data["spec_version"] = version
    return data | kw


def _spec(eid: str = "XP-30", **kw: Any) -> ExperimentSpec:
    return fill_defaults(ExperimentSpec.model_validate(_doc(eid, **kw)), ExperimentDefaults())


def _ab(eid: str, overlays: list[dict[str, Any]]) -> ExperimentSpec:
    doc = _doc(eid, kind="ab", area="exits", backtest_ref="x", non_inferiority_margin=0.1)
    doc["arms"] = {"treatments": {f"t{i}": {"overlay": o} for i, o in enumerate(overlays, 1)}}
    return fill_defaults(ExperimentSpec.model_validate(doc), ExperimentDefaults())


def _shared() -> RunnerConfig:
    return RunnerConfig(
        account_mode="shared",
        arms={
            "t": ArmRunner(
                spec_arm="treatments", keys_env="ALPACA_EXP", db="exp-{experiment_id}-{arm}.db"
            ),
            CONTROL_RUNNER: ArmRunner(
                spec_arm="control", keys_env="ALPACA_EXP", db="exp-{experiment_id}-control.db"
            ),
        },
    )


def _probe(equity: str = "100000") -> Any:
    def probe(keys_env: str) -> ArmAccount:
        _ = keys_env
        return ArmAccount(account_number=ACCT, equity=D(equity), positions=0, open_orders=0)

    return probe


def _registered(conn: sqlite3.Connection, spec: ExperimentSpec, runner: RunnerConfig) -> None:
    store = ExperimentStore.for_runner(conn, runner)
    store.create(spec, actor="local", owner_approval="P-1")
    assert store.register(spec.id, actor="local", owner_approval="P-1").status is (
        ExperimentStatus.REGISTERED
    )


def _start(conn: sqlite3.Connection, eid: str, runner: RunnerConfig, tmp: Path, **kw: Any) -> Any:
    return start_arms(
        conn,
        eid,
        actor="local",
        now=T0,
        t0_equity=D(kw.pop("t0", "20000")),
        runner=runner,
        arm_dir=tmp / "arms",
        control_sha="abcdef1",
        aa_override=True,
        probe=kw.pop("probe", _probe()),
        **kw,
    )


def _open_production_structure(conn: sqlite3.Connection) -> str:
    now = dt.datetime(2026, 9, 25, 16, 0, tzinfo=ET)
    st = credit_vertical(
        "put", "IWM", dt.date(2026, 10, 30), short_strike=211, short_premium="5.60",
        long_strike=210, long_premium="4.70", as_of=now.date(),
    )  # fmt: skip
    cid = CandidateRepo(conn).insert(
        ticker="IWM", stance="bullish", catalyst_type="t", confidence=0.7
    )
    ProposalRepo(conn).insert(
        candidate_id=cid, proposal_hash="a" * 64, structure_json=st.model_dump_json(),
        thesis="t", quant_json="{}", sizing_json="{}", expires_at=now.isoformat(), ticker="IWM",
    )  # fmt: skip
    sid = OpenStructureRepo(conn).open(
        ticker="IWM", open_proposal_hash="a" * 64, candidate_id=cid,
        structure_json=st.model_dump_json(), contracts=2, entry_net=D("-0.90"),
        now=now - dt.timedelta(days=5),
    )  # fmt: skip
    conn.commit()
    return sid


# --- spec versions ----------------------------------------------------------------------


def test_v1_spec_keeps_its_hash_and_its_production_control() -> None:
    sp = load_spec(XP1)
    assert sp.spec_version == 1 and not sp.control_runs
    assert spec_hash(sp) == XP1_HASH
    assert sp.arm_id_for(CONTROL_ARM) is PRODUCTION_ARM is None
    assert sp.arm_id_for("treatment") == "XP-1:t1"


def test_a_spec_without_a_version_stays_v2() -> None:
    sp = ExperimentSpec.model_validate(_doc(version=None))
    assert sp.spec_version == 2 and not sp.control_runs
    assert sp.arm_id_for(CONTROL_ARM) is None
    assert '"spec_version":2' in canonical_json(sp)


def test_v2_and_v3_hash_differ_only_by_version() -> None:
    v2, v3 = _spec(version=2), _spec(version=3)
    assert SPEC_VERSION == 3
    assert spec_hash(v2) != spec_hash(v3)
    assert canonical_json(v2).replace('"spec_version":2', '"spec_version":3') == canonical_json(v3)
    # a v3 spec round-trips through its stored canonical JSON with the same hash
    again = ExperimentSpec.model_validate_json(canonical_json(v3))
    assert again.spec_version == 3 and spec_hash(again) == spec_hash(v3)


def test_v3_control_is_its_own_arm() -> None:
    sp = _spec("XP-31")
    assert sp.control_runs
    assert sp.arm_id_for(CONTROL_ARM) == "XP-31:control"
    assert sp.arm_id_for("t2") == "XP-31:t2"
    assert arm_id("XP-31", CONTROL_ARM, spec_version=3) == "XP-31:control"
    assert arm_id("XP-31", CONTROL_ARM) is None  # the pre-D108 default: production


@pytest.mark.parametrize("version", [1, 2, 3])
def test_a_non_empty_control_overlay_is_refused(version: int) -> None:
    doc = _doc(version=version, k=1)
    doc["arms"] = {"control": {"overlay": EXITS}, "treatments": {"t1": {"overlay": {}}}}
    if version == 1:
        doc["arms"] = {"control": {"overlay": EXITS}, "treatment": {"overlay": {}}}
    with pytest.raises(ValidationError, match="control arm runs production's config"):
        ExperimentSpec.model_validate(doc)


def test_create_refuses_a_v3_spec_with_a_control_overlay(tmp_path: Path) -> None:
    from arc.cli import main

    doc = _doc("XP-32")
    doc["arms"]["control"] = {"overlay": EXITS}
    p = tmp_path / "spec.yaml"
    p.write_text(yaml.safe_dump(doc))
    db = tmp_path / "arc.db"
    argv = ["experiment", "create", "--owner-approval", "P-1", "--spec", str(p), "--db", str(db)]
    assert main(argv) != 0
    conn = _db(db)
    assert ExperimentStore(conn).get("XP-32") is None


# --- RunningDetail ----------------------------------------------------------------------


def test_running_detail_reads_a_stored_legacy_book_as_production_book() -> None:
    stored = {"t0": T0.isoformat(), "t0_equity": 1000.0, "legacy_book": ["os-1"],
              "control_sha": "abcdef1"}  # fmt: skip
    d = RunningDetail.model_validate(stored)
    assert d.production_book == ["os-1"]
    dumped = d.model_dump(mode="json")
    assert dumped["production_book"] == ["os-1"] and "legacy_book" not in dumped
    assert RunningDetail.model_validate(dumped).production_book == ["os-1"]


# --- arms_for ---------------------------------------------------------------------------


def test_arms_for_v3_runs_control_first_then_every_treatment() -> None:
    arms = _shared().arms_for(_spec("XP-33"))
    assert list(arms) == ["control", "t1", "t2", "t3"]
    assert arms["control"].spec_arm == "control"
    assert {a.keys_env for a in arms.values()} == {"ALPACA_EXP"}


def test_arms_for_v1_v2_never_run_control() -> None:
    runner = _shared()
    assert list(runner.arms_for(_spec("XP-34", version=2))) == ["t1", "t2", "t3"]
    assert list(runner.arms_for(load_spec(XP1))) == ["t1"]


def test_arms_for_v3_needs_a_control_runner_arm_on_the_same_account() -> None:
    no_ctl = RunnerConfig(account_mode="shared", arms={"t": _shared().arms["t"]})
    with pytest.raises(ValueError, match="its control runs as arm 'control'"):
        no_ctl.arms_for(_spec("XP-35"))
    other = _shared().model_copy(
        update={
            "arms": {
                **_shared().arms,
                CONTROL_RUNNER: ArmRunner(
                    spec_arm="control", keys_env="ALPACA_OTHER", db="c-{experiment_id}.db"
                ),
            }
        }
    )
    with pytest.raises(ValueError, match="must share one account"):
        other.arms_for(_spec("XP-35"))


def test_a_runner_arm_named_control_must_run_spec_control() -> None:
    with pytest.raises(ValidationError, match="spec_arm must be 'control'"):
        RunnerConfig(
            account_mode="shared",
            arms={CONTROL_RUNNER: ArmRunner(spec_arm="t1", keys_env="ALPACA_EXP", db="x.db")},
        )


def test_a_v3_spec_refuses_a_second_runner_for_control() -> None:
    runner = _shared().model_copy(
        update={
            "arms": {
                **_shared().arms,
                "shadow": ArmRunner(spec_arm="control", keys_env="ALPACA_EXP", db="s.db"),
            }
        }
    )
    with pytest.raises(ValueError, match="runs spec arm control"):
        runner.arms_for(_spec("XP-36"))
    assert "shadow" in runner.arms_for(_spec("XP-36", version=2))  # pre-D108 shadow control


def test_shipped_config_runs_a_k3_v3_spec_as_four_shared_arms() -> None:
    r = load_experiments_config().runner
    assert r.account_mode == "shared"
    arms = r.arms_for(_spec("XP-40"))
    assert list(arms) == ["control", "t1", "t2", "t3"]
    assert [a.db_path("XP-40") for a in arms.values()] == [
        "data/arc-exp-XP-40-control.db",
        *(f"data/arc-exp-XP-40-t{i}.db" for i in (1, 2, 3)),
    ]
    assert {a.keys_env for a in arms.values()} == {"ALPACA_EXP"}


def test_parallel_arm_cap_counts_the_control_arm(tmp_path: Path) -> None:
    from arc.experiments.store import ExperimentError

    conn = _db(tmp_path / "control.db")
    runner = _shared().model_copy(update={"max_parallel_arms": 3})
    store = ExperimentStore.for_runner(conn, runner)
    # control + t1..t3 = 4 arms > 3: refused at register; a v2 K=3 (3 arms) fits
    store.create(_spec("XP-41"), actor="local", owner_approval="P-1")
    with pytest.raises(ExperimentError, match="runs 4 arms, more than max_parallel_arms 3"):
        store.register("XP-41", actor="local", owner_approval="P-1")
    store.create(_spec("XP-48", version=2), actor="local", owner_approval="P-1")
    assert store.register("XP-48", actor="local", owner_approval="P-1").status is (
        ExperimentStatus.REGISTERED
    )


# --- t0: flat start, production book, codes ---------------------------------------------


def test_v3_start_is_flat_in_every_arm_and_records_the_production_book(tmp_path: Path) -> None:
    conn = _db(tmp_path / "control.db")
    sid = _open_production_structure(conn)
    runner = _shared()
    _registered(conn, _spec("XP-42"), runner)
    st = _start(conn, "XP-42", runner, tmp_path)
    assert st.running is not None
    assert st.running.production_book == [sid]
    assert st.running.t0_equity == 20000
    stores = arm_stores(conn, "XP-42")
    assert list(stores) == ["control", "t1", "t2", "t3"]
    codes = set()
    for name, path in stores.items():
        arm = _db(path)
        ident = read_identity(arm)
        assert ident is not None and ident.arm_id == f"XP-42:{name}"
        assert ident.spec_arm == name and ident.overlay == {}
        assert ident.account_mode == "shared" and ident.keys_env == "ALPACA_EXP"
        codes.add(ident.arm_code)
        ledger = [(r["kind"], r["amount"]) for r in arm.execute("SELECT * FROM virtual_ledger")]
        assert ledger == [("open", "20000")]  # no legacy_hold: flat, full buying power
        arm.close()
    assert len(codes) == 4 and None not in codes  # one arm code each, control included
    ev = [e for e in st.events if e.status is ExperimentStatus.RUNNING][-1]
    assert ev.detail["production_book"] == [sid] and "legacy_book" not in ev.detail


def test_v2_start_still_reserves_the_legacy_book(tmp_path: Path) -> None:
    conn = _db(tmp_path / "control.db")
    sid = _open_production_structure(conn)
    runner = _shared()
    _registered(conn, _spec("XP-43", version=2, k=2), runner)
    st = _start(conn, "XP-43", runner, tmp_path)
    assert st.running is not None and st.running.production_book == [sid]
    stores = arm_stores(conn, "XP-43")
    assert list(stores) == ["t1", "t2"]  # production is control: no control arm
    for path in stores.values():
        arm = _db(path)
        kinds = [r["kind"] for r in arm.execute("SELECT kind FROM virtual_ledger ORDER BY rowid")]
        assert kinds == ["open", "legacy_hold"]
        arm.close()


def test_v3_capacity_check_counts_the_control_arm(tmp_path: Path) -> None:
    conn = _db(tmp_path / "control.db")
    runner = _shared()
    _registered(conn, _spec("XP-44"), runner)
    # 4 arms x 20k = 80k > 80k x 0.9 = 72k
    with pytest.raises(ArmStartError, match=r"4 new x \$20,000.00 = \$80,000.00"):
        _start(conn, "XP-44", runner, tmp_path, probe=_probe("80000"))
    assert arm_stores(conn) == {}
    _start(conn, "XP-44", runner, tmp_path, probe=_probe("90000"))


# --- fork rule --------------------------------------------------------------------------


def test_control_fork_is_the_earliest_fork() -> None:
    chain = ["research", "exits.mandatory", "quant.exit", "quant.open", "quant.propose"]
    assert control_fork(chain, "exits.mandatory", ["exits.mandatory", "research"]) == "research"
    assert control_fork(chain, "exits.mandatory", ["quant.propose"]) == "exits.mandatory"
    assert control_fork(chain, "quant.propose", []) == "quant.propose"
    # a fork outside the chain is ignored (the pair step needs a step of this chain)
    assert control_fork(chain, "exits.mandatory", ["new.step"]) == "exits.mandatory"


def test_v3_control_plan_forks_at_the_earliest_treatment_fork(tmp_path: Path) -> None:
    conn = _db(tmp_path / "control.db")
    runner = _shared()
    sp = _ab("XP-45", [EXITS, UNIVERSE, EXITS])
    _registered(conn, sp, runner)
    st = _start(conn, "XP-45", runner, tmp_path)
    assert st.running is not None
    forks = {n: p.fork_step for n, p in st.running.arm_plans.items()}
    assert forks == {
        "control": "research",  # t2's universe overlay forks at research
        "t1": "exits.mandatory",
        "t2": "research",
        "t3": "exits.mandatory",
    }
    # the plan stored on the control arm's identity is the same one
    ctl = _db(arm_stores(conn, "XP-45")["control"])
    ident = read_identity(ctl)
    assert ident is not None and ident.plan is not None
    assert ident.plan["fork_step"] == "research"
    assert ident.plan["arm_personas"] == []  # control runs no persona of its own
    ctl.close()
    # an all-exits v3 experiment: control forks where every treatment does
    conn2 = _db(tmp_path / "c2.db")
    _registered(conn2, _ab("XP-46", [EXITS, EXITS, EXITS]), runner)
    st2 = _start(conn2, "XP-46", runner, tmp_path / "b")
    assert st2.running is not None
    assert {p.fork_step for p in st2.running.arm_plans.values()} == {"exits.mandatory"}


def test_start_cli_json_names_the_production_book(tmp_path: Path, capsys: Any) -> None:
    from arc.cli import main

    db = tmp_path / "arc.db"
    conn = _db(db)
    sid = _open_production_structure(conn)
    conn.close()
    p = tmp_path / "spec.yaml"
    p.write_text(yaml.safe_dump(_doc("XP-47")))
    for argv in (
        ["experiment", "create", "--owner-approval", "P-1", "--spec", str(p)],
        ["experiment", "register", "--owner-approval", "P-1", "XP-47"],
    ):
        assert main([*argv, "--db", str(db)]) == 0
    capsys.readouterr()
    assert (
        main(["experiment", "start", "XP-47", "--fixtures", "--t0-equity", "20000",
              "--arm-dir", str(tmp_path / "arms"), "--now", T0.isoformat(), "--json",
              "--db", str(db)])
        == 0
    )  # fmt: skip
    out = json.loads(capsys.readouterr().out)
    assert out["production_book"] == [sid]
    assert list(out["arms"]) == ["control", "t1", "t2", "t3"]
