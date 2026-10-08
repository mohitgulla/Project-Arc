"""Control tower v2 API (E8.7, D35): read-only FastAPI over the audit store."""

from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from arc.cli import main
from arc.control.store import ConfigChangeRepo
from arc.store.db import connect
from arc.tower import net
from arc.tower.api import create_app
from arc.tower.routes.meta import _every_s, cadences
from arc.tower.schemas import MetaResponse
from arc.tower.serve import app_from_env, uvicorn_command
from arc.utils.calendar import ET
from tests.test_tower import NOW, db  # noqa: F401 - fixture re-export

REPO = Path(__file__).resolve().parent.parent


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture
def static(tmp_path: Path) -> Path:
    d = tmp_path / "static"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html><div id=root>arc</div>")
    (d / "assets" / "app.js").write_text("console.log(1)")
    return d


@pytest.fixture
def client(db: Path, static: Path) -> TestClient:  # noqa: F811
    return TestClient(create_app(db, static_dir=static, clock=lambda: NOW))


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------


def test_health(client: TestClient, db: Path) -> None:  # noqa: F811
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["db"] == str(db.resolve())
    assert dt.datetime.fromisoformat(body["as_of"]) == NOW
    assert body["as_of"].endswith("-04:00")  # ET


def test_meta_has_cadences_caps_and_env(client: TestClient) -> None:
    r = client.get("/api/meta")
    assert r.status_code == 200
    m = MetaResponse.model_validate(r.json())
    assert set(m.cadences) == {"monitor", "broker.reconcile", "tick", "health"}
    assert m.cadences["tick"].every_s == 600 and m.cadences["tick"].stale_after_s == 1800  # D52
    assert m.cadences["health"].every_s == 1800  # E8.8b status row: the LaunchAgent cadence
    mon = m.cadences["monitor"]
    assert mon.stale_after_s == 3 * mon.every_s and mon.window and mon.days == "trading"
    assert m.cadences["broker.reconcile"].every_s == 86_400  # one run a day
    assert m.env == "paper" and m.account_profile
    assert m.gate_caps.portfolio_dollar_delta_cap_pct == pytest.approx(1.00)  # D62
    assert m.gate_caps.portfolio_beta_delta_cap_pct == pytest.approx(2.00)
    assert m.gate_caps.portfolio_vega_cap_pct == pytest.approx(0.010)
    assert m.refresh_interval_s == 60 and m.refresh_choices_s == [30, 60, 120]
    assert m.app.name == "arc" and m.app.version and m.theme_default == "system"
    assert m.as_of == NOW


def test_meta_and_snapshot_read_d26_overrides_from_store(db: Path, static: Path) -> None:  # noqa: F811
    """A Slack config change (D26) reaches the tower on the next request, no restart."""
    c = TestClient(create_app(db, static_dir=static, clock=lambda: NOW))
    assert c.get("/api/meta").json()["config_version"] == 0
    conn = connect(db)
    ConfigChangeRepo(conn).append(
        key="portfolio_dollar_delta_cap_pct", old=1.00, new=0.20, is_default=False, actor="U1",
        reason="test", at=NOW, source="slack", status="applied", direction="safer",
    )  # fmt: skip
    conn.close()
    before = _sha(db)
    m = c.get("/api/meta").json()
    assert m["config_version"] == 1
    assert m["gate_caps"]["portfolio_dollar_delta_cap_pct"] == pytest.approx(0.20)
    s = c.get("/api/snapshot").json()
    assert s["greeks"]["dollar_delta_cap"] == pytest.approx(0.20 * 100500)
    # D62: the beta-weighted cap follows its own override too
    conn = connect(db)
    ConfigChangeRepo(conn).append(
        key="portfolio_beta_delta_cap_pct", old=2.00, new=1.50, is_default=False, actor="U1",
        reason="test", at=NOW, source="slack", status="applied", direction="safer",
    )  # fmt: skip
    conn.close()
    before = _sha(db)
    assert c.get("/api/meta").json()["gate_caps"]["portfolio_beta_delta_cap_pct"] == 1.50
    s = c.get("/api/snapshot").json()
    assert s["greeks"]["beta_delta_cap"] == pytest.approx(1.50 * 100500)
    assert _sha(db) == before


def test_snapshot_is_the_tower_snapshot(client: TestClient) -> None:
    r = client.get("/api/snapshot")
    assert r.status_code == 200
    s = r.json()
    assert dt.datetime.fromisoformat(s["as_of"]) == NOW
    assert s["greeks"]["delta"] == 25.0 and s["greeks"]["dollar_delta_cap"] == pytest.approx(
        100_500
    )
    assert {p["ticker"] for p in s["proposals"]} == {"SPY", "QQQ"}
    wide = client.get("/api/snapshot", params={"lookback_days": 30}).json()
    assert {p["ticker"] for p in wide["proposals"]} == {"SPY", "QQQ", "IWM"}
    bad = client.get("/api/snapshot", params={"lookback_days": 0})
    assert bad.status_code == 422 and bad.json()["error"] == "invalid_request"


def test_missing_db_is_a_503_error_body(tmp_path: Path, static: Path) -> None:
    missing = tmp_path / "nope.db"
    c = TestClient(create_app(missing, static_dir=static, clock=lambda: NOW))
    for path in ("/api/health", "/api/meta", "/api/snapshot"):
        r = c.get(path)
        assert r.status_code == 503, path
        body = r.json()
        assert body["error"] == "db_unavailable" and "not found" in body["detail"]
        assert dt.datetime.fromisoformat(body["as_of"]) == NOW
    assert not missing.exists()


def test_unknown_api_route_never_falls_back_to_spa(client: TestClient) -> None:
    r = client.get("/api/nope")
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    assert r.headers["content-type"].startswith("application/json")
    r = client.post("/api/health")
    assert r.status_code == 405 and r.json()["error"] == "method_not_allowed"


def test_spa_serves_index_assets_and_client_routes(client: TestClient) -> None:
    for path in ("/", "/kitchen-sink", "/trades/abc"):
        r = client.get(path)
        assert r.status_code == 200 and "id=root" in r.text, path
    assert client.get("/assets/app.js").text == "console.log(1)"
    assert client.get("/assets/missing.js").status_code == 404
    assert client.get("/../pyproject.toml").status_code in (404, 200)
    assert "tool.importlinter" not in client.get("/%2e%2e/%2e%2e/pyproject.toml").text


def test_spa_not_built_is_a_clear_503(db: Path, tmp_path: Path) -> None:  # noqa: F811
    c = TestClient(create_app(db, static_dir=tmp_path / "empty", clock=lambda: NOW))
    r = c.get("/")
    assert r.status_code == 503 and "make web" in r.text
    assert c.get("/api/health").status_code == 200


def test_no_cors_headers(client: TestClient) -> None:
    r = client.get("/api/health", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


# ---------------------------------------------------------------------------
# read-only by construction
# ---------------------------------------------------------------------------


def test_openapi_has_only_get_routes(client: TestClient) -> None:
    spec = client.get("/api/openapi.json").json()
    methods = {m for ops in spec["paths"].values() for m in ops}
    assert methods == {"get"}
    assert {"/api/health", "/api/meta", "/api/snapshot"} <= set(spec["paths"])


def test_app_routes_are_get_or_head_only(db: Path) -> None:  # noqa: F811
    app = create_app(db)
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        assert methods <= {"GET", "HEAD"}, (route.path, methods)


def test_db_byte_identical_after_a_session(db: Path, static: Path) -> None:  # noqa: F811
    before = _sha(db)
    with TestClient(create_app(db, static_dir=static)) as c:
        for _ in range(3):
            for path in ("/api/health", "/api/meta", "/api/snapshot", "/", "/kitchen-sink"):
                assert c.get(path).status_code == 200, path
    assert _sha(db) == before
    assert not Path(f"{db}-wal").exists() or Path(f"{db}-wal").stat().st_size == 0


def test_snapshot_issues_only_selects(db: Path, static: Path) -> None:  # noqa: F811
    import arc.tower.data as data

    seen: list[str] = []
    real = data.connect_ro

    def traced(path: object) -> object:
        conn = real(path)  # type: ignore[arg-type]
        conn.set_trace_callback(seen.append)
        return conn

    with mock.patch("arc.tower.routes.deps.connect_ro", side_effect=traced):
        c = TestClient(create_app(db, static_dir=static, clock=lambda: NOW))
        assert c.get("/api/snapshot").status_code == 200
        assert c.get("/api/health").status_code == 200
    stmts = {s.strip().split()[0].upper() for s in seen if s.strip()}
    assert stmts and stmts <= {"SELECT", "PRAGMA"}


def test_tower_package_has_no_write_sql_or_banned_imports() -> None:
    banned = ("arc.broker", "arc.data", "arc.personas", "arc.llm_routing", "alpaca", "slack_sdk")
    for f in (REPO / "arc" / "tower").rglob("*.py"):
        text = f.read_text()
        for b in banned:
            assert f"import {b}" not in text and f"from {b} " not in text, (f.name, b)
        for kw in ("INSERT", "UPDATE ", "DELETE", "CREATE TABLE", "DROP "):
            assert kw not in text, (f.name, kw)
        for verb in ("post", "put", "patch", "delete"):
            assert f".{verb}(" not in text, (f.name, verb)


def test_import_linter_contract_for_tower() -> None:
    text = (REPO / "pyproject.toml").read_text()
    assert 'source_modules = ["arc.tower"]' in text
    block = text.split('source_modules = ["arc.tower"]', 1)[1].split("[[", 1)[0]
    for mod in ("arc.broker", "arc.data", "arc.personas", "arc.llm_routing", "alpaca", "slack_sdk"):
        assert f'"{mod}"' in block, mod


# ---------------------------------------------------------------------------
# cadences
# ---------------------------------------------------------------------------


def test_every_s_from_every_schedule_and_trigger() -> None:
    from arc.routines.config import JobSpec

    assert _every_s(JobSpec(every="30m")) == 1800
    assert _every_s(JobSpec(schedule=["16:30"])) == 86_400
    assert _every_s(JobSpec(schedule=["12:00", "22:00"])) == 10 * 3600
    assert _every_s(JobSpec(trigger="approval")) == 86_400


def test_cadences_skip_missing_jobs() -> None:
    from arc.routines.config import RoutinesConfig

    out = cadences(RoutinesConfig())
    assert set(out) == {"tick", "health"} and out["tick"].stale_after_s == 900


# ---------------------------------------------------------------------------
# serve: Tailscale or loopback bind only (D29)
# ---------------------------------------------------------------------------


def test_uvicorn_command_is_hardened() -> None:
    argv = uvicorn_command("100.77.0.5", 4174)
    assert argv[1:4] == ["-m", "uvicorn", "arc.tower.serve:app_from_env"]
    assert "--factory" in argv and "--no-proxy-headers" in argv and "--no-server-header" in argv
    assert argv[argv.index("--host") + 1] == "100.77.0.5"
    assert argv[argv.index("--port") + 1] == "4174"
    assert argv[argv.index("--workers") + 1] == "1" and "--reload" not in argv


def test_cli_serve_binds_tailscale(db: Path, capsys: pytest.CaptureFixture[str]) -> None:  # noqa: F811
    with (
        mock.patch("arc.tower.net.resolve_bind_address", return_value="100.77.0.5"),
        mock.patch("arc.tower.cli.subprocess.call", return_value=0) as call,
    ):
        assert main(["tower", "serve", "--db", str(db)]) == 0
    argv = call.call_args.args[0]
    env = call.call_args.kwargs["env"]
    assert "uvicorn" in argv and argv[argv.index("--host") + 1] == "100.77.0.5"
    assert env["ARC_TOWER_DB"] == str(db.resolve()) and env["ARC_TOWER_REFRESH"] == "60"
    assert "arc tower: http://100.77.0.5:4174" in capsys.readouterr().out


@pytest.mark.parametrize("address", ["0.0.0.0", "192.168.1.5", "10.0.0.10", "::"])
def test_cli_serve_refuses_public_or_lan(
    db: Path,  # noqa: F811
    address: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with mock.patch("arc.tower.cli.subprocess.call") as call:
        assert main(["tower", "serve", "--db", str(db), "--address", address]) == 2
    call.assert_not_called()
    assert "refusing to bind" in capsys.readouterr().out


def test_cli_serve_refuses_without_tailscale(
    db: Path,  # noqa: F811
    capsys: pytest.CaptureFixture[str],
) -> None:
    err = net.NoTailscaleAddressError("no Tailscale IPv4 address found")
    with (
        mock.patch("arc.tower.net.resolve_bind_address", side_effect=err),
        mock.patch("arc.tower.cli.subprocess.call") as call,
    ):
        assert main(["tower", "serve", "--db", str(db)]) == 2
    call.assert_not_called()
    assert "no Tailscale" in capsys.readouterr().out


def test_cli_serve_missing_db_and_print_command(
    db: Path,  # noqa: F811
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "missing.db"
    with mock.patch("arc.tower.cli.subprocess.call") as call:
        assert main(["tower", "serve", "--db", str(missing), "--local"]) == 2
    call.assert_not_called()
    assert not missing.exists()
    capsys.readouterr()
    assert main(["tower", "serve", "--local", "--db", str(db), "--print-command"]) == 0
    out = capsys.readouterr().out
    assert "uvicorn arc.tower.serve:app_from_env --factory --host 127.0.0.1 --port 4174" in out


def test_cli_serve_refresh_choices(db: Path, capsys: pytest.CaptureFixture[str]) -> None:  # noqa: F811
    base = ["tower", "serve", "--local", "--db", str(db), "--print-command"]
    assert main([*base, "--refresh", "45"]) == 2
    assert "must be one of [30, 60, 120]" in capsys.readouterr().out
    with mock.patch("arc.tower.cli.subprocess.call", return_value=0) as call:
        assert main(["tower", "serve", "--local", "--db", str(db), "--refresh", "30"]) == 0
    assert call.call_args.kwargs["env"]["ARC_TOWER_REFRESH"] == "30"


def test_app_from_env(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setenv("ARC_TOWER_DB", str(db))
    monkeypatch.setenv("ARC_TOWER_REFRESH", "120")
    monkeypatch.setenv("ARC_TOWER_LOOKBACK_DAYS", "14")
    app = app_from_env()
    assert app.state.tower.db_path == db.resolve()
    assert app.state.tower.refresh_s == 120 and app.state.tower.lookback_days == 14
    with TestClient(app) as c:
        m = c.get("/api/meta").json()
    assert m["refresh_interval_s"] == 120 and m["lookback_days"] == 14
    as_of = dt.datetime.fromisoformat(m["as_of"])
    assert as_of.utcoffset() == as_of.astimezone(ET).utcoffset()  # ET wall clock


# ---------------------------------------------------------------------------
# the SPA's typed client is generated from this spec: it must not drift
# ---------------------------------------------------------------------------


def test_committed_openapi_matches_the_app() -> None:
    """``web/openapi.json`` is what ``npm run gen:api`` types the client from.

    After changing a route or response model: ``make web-api`` and commit both files.
    """
    from arc.tower.openapi import render

    committed = (REPO / "web" / "openapi.json").read_text()
    assert committed == render(), "web/openapi.json is stale: run `make web-api`"


def test_openapi_cli_writes_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from arc.tower.openapi import main as openapi_main

    out = tmp_path / "spec.json"
    assert openapi_main([str(out)]) == 0
    assert '"/api/meta"' in out.read_text()
    assert openapi_main([]) == 0
    assert '"/api/health"' in capsys.readouterr().out
