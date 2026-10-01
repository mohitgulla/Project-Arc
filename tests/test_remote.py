"""E8.6 remote access over Tailscale: dashboard preflight, env writer, scripts, health check."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import textwrap
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from arc.cli import main
from arc.monitoring import alerts, checks
from arc.monitoring.checks import CheckResult
from arc.monitoring.config import MonitoringSettings, RemoteAccessCheck
from arc.remote import BASIC_AUTH_KEYS, PLAINTEXT_PASSWORD_KEY, cli, envfile
from arc.routines.config import load_routines
from arc.store.db import connect
from arc.store.migrate import migrate
from arc.tower import net

REPO = Path(__file__).resolve().parent.parent
REMOTE = REPO / "hermes" / "remote"
FAKE_HASH = "scrypt$16384$8$1$c2FsdHNhbHQ=$ZGtka2Rr"
AUTH_ENV = {
    "HERMES_DASHBOARD_BASIC_AUTH_USERNAME": "owner",
    "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH": FAKE_HASH,
    "HERMES_DASHBOARD_BASIC_AUTH_SECRET": "c2VjcmV0c2VjcmV0c2VjcmV0",
}
NOT_RUNNING = (0, "No hermes dashboard or serve processes running.\n")


def _args(**kw: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "port": 1994,
        "address": None,
        "local": False,
        "env_file": None,
        "hermes_bin": "/x/hermes",
        "print_command": False,
    }
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    p = tmp_path / ".env"
    p.write_text("".join(f"{k}='{v}'\n" for k, v in AUTH_ENV.items()))
    return p


# ---------------------------------------------------------------------------
# bind resolution (shared by both services)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["0.0.0.0", "192.168.1.5", "10.0.0.2", "mac-mini", "::", ""])
def test_dashboard_refuses_wide_or_named_binds(bad: str, env_file: Path) -> None:
    args = _args(address=bad or None, env_file=str(env_file))
    with (
        mock.patch.object(net, "_tailscale_cli", return_value=None),
        mock.patch.object(net.shutil, "which", return_value=None),
        pytest.raises(cli.PreflightError, match="refusing to bind|no Tailscale"),
    ):
        cli.preflight(args, environ={}, status=lambda h: NOT_RUNNING, probe=lambda h, p: False)


def test_tower_refuses_same_binds(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "arc.db"
    migrate(connect(db))
    for bad in ("0.0.0.0", "192.168.1.5", "mac-mini"):
        assert main(["tower", "serve", "--db", str(db), "--address", bad]) == 2
        assert "refusing to bind" in capsys.readouterr().out


def test_dashboard_uses_tailscale_address(env_file: Path) -> None:
    with (
        mock.patch("arc.tower.net.resolve_bind_address", return_value="100.77.0.5"),
        mock.patch.object(cli, "resolve_hermes", return_value="/x/hermes"),
    ):
        argv = cli.preflight(
            _args(env_file=str(env_file)),
            environ={},
            status=lambda h: NOT_RUNNING,
            probe=lambda h, p: False,
        )
    assert argv == [
        "/x/hermes", "dashboard", "--host", "100.77.0.5", "--port", "1994",
        "--no-open", "--skip-build",
    ]  # fmt: skip


def test_dashboard_no_tailscale_exits_2(env_file: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with (
        mock.patch.object(net, "_tailscale_cli", return_value=None),
        mock.patch.object(net, "_run", return_value="inet 192.168.1.20 netmask"),
    ):
        rc = main(["remote", "dashboard", "--env-file", str(env_file)])
    assert rc == 2
    assert "no Tailscale IPv4 address" in capsys.readouterr().out


def test_lan_addresses_exclude_loopback_and_tailscale() -> None:
    text = "inet 127.0.0.1 netmask\ninet 192.168.1.20 netmask\ninet 100.64.1.2\ninet 0.0.0.0\n"
    assert net.lan_addresses(text + "inet 192.168.1.20\ninet 10.1.2.3") == [
        "192.168.1.20",
        "10.1.2.3",
    ]
    with mock.patch.object(net.shutil, "which", return_value=None):
        assert net.host_lan_addresses() == []
    with (
        mock.patch.object(net.shutil, "which", return_value="/sbin/ifconfig"),
        mock.patch.object(net, "_run", return_value=text),
    ):
        assert net.host_lan_addresses() == ["192.168.1.20"]


# ---------------------------------------------------------------------------
# preflight: basic auth + second copy
# ---------------------------------------------------------------------------


def test_basic_auth_missing_keys(tmp_path: Path) -> None:
    empty = tmp_path / "none.env"
    with pytest.raises(cli.PreflightError) as exc:
        cli.check_basic_auth(empty, {})
    msg = str(exc.value)
    assert all(k in msg for k in BASIC_AUTH_KEYS) and "set-password.sh" in msg
    # Keys from the process env count; blank values do not.
    cli.check_basic_auth(empty, AUTH_ENV)
    blank = tmp_path / "blank.env"
    blank.write_text("".join(f"{k}=''\n" for k in BASIC_AUTH_KEYS))
    with pytest.raises(cli.PreflightError, match="missing"):
        cli.check_basic_auth(blank, {})


def test_basic_auth_refuses_plaintext_password(env_file: Path) -> None:
    cli.check_basic_auth(env_file, {})
    with pytest.raises(cli.PreflightError, match="plaintext"):
        cli.check_basic_auth(env_file, {PLAINTEXT_PASSWORD_KEY: "hunter2"})
    env_file.write_text(env_file.read_text() + f"export {PLAINTEXT_PASSWORD_KEY}=hunter2\n")
    with pytest.raises(cli.PreflightError, match="plaintext") as exc:
        cli.check_basic_auth(env_file, {})
    assert "hunter2" not in str(exc.value)


def test_read_dotenv_keys_never_returns_values(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_text("# c\n\nA='x'\nexport B=\"\"\nC=  \nnoequals\n")
    assert cli.read_dotenv_keys(p) == {"A": True, "B": False, "C": False}


def test_second_copy_refused() -> None:
    running = (0, "1 hermes dashboard/serve process(es) running:\n    PID 9 [dashboard]: ...")
    with pytest.raises(cli.PreflightError, match="already running"):
        cli.check_not_running("h", "100.64.0.9", 1994, status=lambda h: running)
    with pytest.raises(cli.PreflightError, match="exited 1"):
        cli.check_not_running("h", "100.64.0.9", 1994, status=lambda h: (1, "boom"))
    # `--status` misses launcher-started dashboards; the port probe catches them.
    seen: list[str] = []

    def busy_on_ts(host: str, port: int) -> bool:
        seen.append(host)
        return host == "100.64.0.9"

    with pytest.raises(cli.PreflightError, match="port 1994 already in use on 100.64.0.9"):
        cli.check_not_running("h", "100.64.0.9", 1994, status=lambda h: NOT_RUNNING,
                              probe=busy_on_ts)  # fmt: skip
    assert seen == ["127.0.0.1", "100.64.0.9"]
    cli.check_not_running("h", "127.0.0.1", 1994, status=lambda h: NOT_RUNNING,
                          probe=lambda h, p: False)  # fmt: skip


def test_preflight_missing_hermes(env_file: Path) -> None:
    with pytest.raises(cli.PreflightError, match="hermes CLI not found"):
        cli.preflight(_args(local=True, env_file=str(env_file), hermes_bin="/nope/hermes"),
                      environ={})  # fmt: skip


def test_cli_print_command_and_exec(
    env_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hermes = tmp_path / "hermes"
    hermes.write_text("#!/bin/sh\necho 'No hermes dashboard or serve processes running.'\n")
    hermes.chmod(0o755)
    base = ["remote", "dashboard", "--local", "--env-file", str(env_file), "--hermes-bin",
            str(hermes)]  # fmt: skip
    with mock.patch.object(cli, "port_open", return_value=False):
        assert main([*base, "--print-command"]) == 0
        assert f"{hermes} dashboard --host 127.0.0.1 --port 1994" in capsys.readouterr().out
        with mock.patch.object(cli.os, "execv") as execv:
            main(base)
    execv.assert_called_once()
    assert execv.call_args.args[1][2:4] == ["--host", "127.0.0.1"]


def test_port_open(tmp_path: Path) -> None:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        assert cli.port_open("127.0.0.1", port)
    assert not cli.port_open("127.0.0.1", port)


# ---------------------------------------------------------------------------
# env writer (set-password.sh's back half)
# ---------------------------------------------------------------------------

U, H, S = BASIC_AUTH_KEYS


def test_envfile_update_sets_replaces_and_keeps_secret() -> None:
    new, changes = envfile.update("OTHER=1\n", "owner", FAKE_HASH)
    assert new.startswith("OTHER=1\n")
    assert f"{U}='owner'" in new and f"{H}='{FAKE_HASH}'" in new
    assert f"set {S}" in changes
    secret_line = next(line for line in new.splitlines() if line.startswith(S))
    # Re-run: username/hash replaced, secret kept byte-identical, no duplicates.
    again, changes2 = envfile.update(new + f"{U}='dupe'\n", "other", FAKE_HASH)
    assert secret_line in again and f"kept {S}" in changes2
    assert again.count(f"{U}=") == 1 and f"{U}='other'" in again
    assert again.count(f"{S}=") == 1


def test_envfile_removes_plaintext_and_blank_secret() -> None:
    text = f"{PLAINTEXT_PASSWORD_KEY}=hunter2\nexport {S}=''\n{S}=\n"
    new, changes = envfile.update(text, "owner", FAKE_HASH)
    assert PLAINTEXT_PASSWORD_KEY + "=" not in new and "hunter2" not in new
    assert new.count(f"{S}=") == 1 and f"{S}=''" not in new
    assert any("plaintext" in c for c in changes)
    assert all("hunter2" not in c and FAKE_HASH not in c for c in changes)


@pytest.mark.parametrize(
    ("user", "hashed"),
    [("", FAKE_HASH), ("bad user", FAKE_HASH), ("owner", "plaintext-pw"), ("owner", "")],
)
def test_envfile_validates(user: str, hashed: str) -> None:
    with pytest.raises(ValueError):
        envfile.update("", user, hashed)


def test_envfile_write_is_0600_and_main(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "sub" / ".env"
    envfile.write_env(p, "owner", FAKE_HASH)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    p.chmod(0o644)
    envfile.write_env(p, "owner", FAKE_HASH)
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(f"owner\n{FAKE_HASH}\n"))
    assert envfile.main([str(p)]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("owner\nnot-a-hash\n"))
    assert envfile.main([str(p)]) == 2
    assert envfile.main([]) == 2


def _fake_runtime(tmp_path: Path) -> Path:
    """Stand-in for the Hermes runtime python: consumes stdin, prints a scrypt-shaped hash."""
    fake = tmp_path / "fake-runtime-python"
    fake.write_text(f"#!/bin/sh\nread -r pw\necho '{FAKE_HASH}'\n")
    fake.chmod(0o755)
    return fake


def _set_password(tmp_path: Path, stdin: str, env_path: Path, runtime: Path) -> Any:
    env = {
        **os.environ,
        "HERMES_ENV_FILE": str(env_path),
        "HERMES_RUNTIME_PYTHON": str(runtime),
        "HERMES_AGENT_DIR": str(tmp_path),
    }
    return subprocess.run(  # noqa: S603
        ["bash", str(REMOTE / "set-password.sh"), str(REPO)],
        input=stdin, capture_output=True, text=True, env=env, timeout=120,
    )  # fmt: skip


def test_set_password_script_never_writes_plaintext(tmp_path: Path) -> None:
    pw = "correct-horse-battery-staple"
    env_path = tmp_path / ".env"
    env_path.write_text(f"KEEP=me\n{PLAINTEXT_PASSWORD_KEY}=old-plain\n")
    env_path.chmod(0o644)
    r = _set_password(tmp_path, f"owner\n{pw}\n{pw}\n", env_path, _fake_runtime(tmp_path))
    assert r.returncode == 0, r.stderr
    text = env_path.read_text()
    assert pw not in text and "old-plain" not in text and PLAINTEXT_PASSWORD_KEY + "=" not in text
    assert "KEEP=me" in text and f"{H}='{FAKE_HASH}'" in text
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert pw not in r.stdout + r.stderr and FAKE_HASH not in r.stdout + r.stderr
    secret = next(line for line in text.splitlines() if line.startswith(S))
    # Idempotent: a second run keeps the signing secret (sessions survive).
    r2 = _set_password(tmp_path, f"owner\n{pw}\n{pw}\n", env_path, _fake_runtime(tmp_path))
    assert r2.returncode == 0 and secret in env_path.read_text()


def test_set_password_script_rejects_mismatch_and_short(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    fake = _fake_runtime(tmp_path)
    r = _set_password(tmp_path, "owner\nlong-password-one\nlong-password-two\n", env_path, fake)
    assert r.returncode != 0 and "do not match" in r.stderr
    r = _set_password(tmp_path, "owner\nshort\nshort\n", env_path, fake)
    assert r.returncode != 0 and "12 characters" in r.stderr
    assert not env_path.exists()


_HERMES_AGENT = Path.home() / ".hermes" / "hermes-agent"


@pytest.mark.skipif(
    not (_HERMES_AGENT / ".hermes" / "bin" / "hermes").exists(), reason="no local Hermes install"
)
def test_hash_password_uses_hermes_runtime(tmp_path: Path) -> None:
    launcher = _HERMES_AGENT / ".hermes" / "bin" / "hermes"
    out = subprocess.run(  # noqa: S603
        [str(launcher), "--print-runtime-command"], capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    runtime = json.loads(out.stdout)[0]
    r = subprocess.run(  # noqa: S603
        [runtime, "-I", str(REMOTE / "hash_password.py"), str(_HERMES_AGENT)],
        input="a-throwaway-password\n", capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr
    assert envfile._HASH.fullmatch(r.stdout.strip())
    assert "a-throwaway-password" not in r.stdout


# ---------------------------------------------------------------------------
# install.sh / run_dashboard.sh
# ---------------------------------------------------------------------------


def test_install_print_shows_both_plists() -> None:
    out = subprocess.run(  # noqa: S603
        ["bash", str(REMOTE / "install.sh"), "--print", str(REPO)],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    assert out.count("<?xml") == 2
    assert "<string>com.projectarc.hermes-dashboard</string>" in out
    assert "<string>com.projectarc.tower</string>" in out
    assert out.count("<key>KeepAlive</key><true/>") == 2
    assert out.count("<key>RunAtLoad</key><true/>") == 2
    assert f"<string>{REPO}/.venv/bin/arc</string>" in out
    assert "<string>tower</string>" in out and "<string>serve</string>" in out
    assert f"<string>{REMOTE}/run_dashboard.sh</string>" in out
    assert (
        "/data/logs/hermes-dashboard.launchd.log" in out and "/data/logs/tower.launchd.log" in out
    )
    assert "/.local/bin</string>" in out
    assert "0.0.0.0" not in out and "/usr/bin/python3" not in out


def _fake_bin(tmp_path: Path, log: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("launchctl", "plutil"):
        f = bin_dir / name
        f.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\n')
        f.chmod(0o755)
    return bin_dir


def test_install_and_uninstall_touch_only_own_labels(tmp_path: Path) -> None:
    log = tmp_path / "calls.log"
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "arc").write_text("#!/bin/sh\n")
    (repo / ".venv" / "bin" / "arc").chmod(0o755)
    (repo / "arc" / "tower" / "static").mkdir(parents=True)
    (repo / "arc" / "tower" / "static" / "index.html").write_text("<!doctype html>")
    env = {**os.environ, "HOME": str(home), "PATH": f"{_fake_bin(tmp_path, log)}:/usr/bin:/bin"}
    for argv in (["install.sh", str(repo)], ["install.sh", "--uninstall"]):
        subprocess.run(  # noqa: S603
            ["bash", str(REMOTE / argv[0]), *argv[1:]], env=env, check=True,
            capture_output=True, text=True, timeout=60,
        )  # fmt: skip
    calls = log.read_text().splitlines()
    touched = [c for c in calls if c.startswith("launchctl bootout") or " bootstrap " in c]
    assert touched, calls
    for c in touched:
        assert "com.projectarc.hermes-dashboard" in c or "com.projectarc.tower" in c, c
    assert not any("ai.hermes.gateway" in c or "health-check" in c for c in calls)
    assert not list((home / "Library" / "LaunchAgents").glob("*.plist"))


def test_install_refuses_without_built_spa(tmp_path: Path) -> None:
    log = tmp_path / "calls.log"
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "arc").write_text("#!/bin/sh\n")
    (repo / ".venv" / "bin" / "arc").chmod(0o755)
    env = {**os.environ, "HOME": str(home), "PATH": f"{_fake_bin(tmp_path, log)}:/usr/bin:/bin"}
    r = subprocess.run(  # noqa: S603
        ["bash", str(REMOTE / "install.sh"), str(repo)], env=env,
        capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert r.returncode == 1
    assert "arc/tower/static/index.html missing" in r.stderr and "make web" in r.stderr
    assert not log.exists()  # nothing loaded, no plist written
    assert not (home / "Library" / "LaunchAgents").exists()


def test_run_dashboard_script_execs_arc_remote(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".venv" / "bin").mkdir(parents=True)
    arc = repo / ".venv" / "bin" / "arc"
    arc.write_text('#!/bin/sh\necho "arc $*"\nexit 2\n')
    arc.chmod(0o755)
    r = subprocess.run(  # noqa: S603
        ["bash", str(REMOTE / "run_dashboard.sh")], env={**os.environ, "ARC_REPO": str(repo)},
        capture_output=True, text=True, timeout=30,
    )  # fmt: skip
    assert r.returncode == 2 and "arc remote dashboard" in r.stdout


# ---------------------------------------------------------------------------
# remote_access health check
# ---------------------------------------------------------------------------

RA = RemoteAccessCheck(enabled=True)
GATED = json.dumps({"auth_required": True, "auth_providers": ["basic"]})


def _get(dash: tuple[int, str], tower: tuple[int, str]) -> Any:
    def get(url: str, timeout: float) -> tuple[int, str]:
        return dash if ":1994/" in url else tower

    return get


TOWER_OK = json.dumps({"status": "ok", "db": "/x/arc.db", "as_of": "2026-09-30T12:00:00-04:00"})


def _run(dash: tuple[int, str] = (200, GATED), tower: tuple[int, str] = (200, TOWER_OK),
         lan: list[str] | None = None, open_: set[str] | None = None) -> CheckResult:  # fmt: skip
    return checks.remote_access(
        RA,
        resolve=lambda: "100.77.0.5",
        get=_get(dash, tower),
        lan=lambda: lan or [],
        probe=lambda h, p, t: f"{h}:{p}" in (open_ or set()),
    )


def _keys(r: CheckResult) -> set[str]:
    return {f.key for f in r.findings}


def test_remote_access_ok() -> None:
    r = _run(lan=["192.168.1.20"])
    assert r.severity == "ok" and not r.findings
    assert "100.77.0.5" in r.summary


@pytest.mark.parametrize(
    ("body", "why"),
    [
        (json.dumps({"auth_required": False, "auth_providers": []}), "WITHOUT authentication"),
        (json.dumps({"auth_required": True, "auth_providers": ["nous"]}), "'basic' not registered"),
        ("<html>", "did not return JSON"),
        ("[]", "JSON object"),
    ],
)
def test_remote_hermes_unauthenticated_is_an_alert(body: str, why: str) -> None:
    r = _run(dash=(200, body))
    assert r.severity == "failed" and _keys(r) == {"remote_hermes"}
    assert why in r.findings[0].message


def test_remote_down() -> None:
    r = _run(dash=(0, "Connection refused"), tower=(503, ""))
    assert _keys(r) == {"remote_hermes", "remote_tower"}
    msgs = " ".join(f.message for f in r.findings)
    assert "no answer (Connection refused)" in msgs and "HTTP 503" in msgs
    r = _run(dash=(401, ""), tower=(0, "timed out"))
    assert "HTTP 401" in r.findings[0].message and "no answer (timed out)" in r.findings[1].message


def test_remote_tower_probes_api_health() -> None:
    urls: list[str] = []

    def get(url: str, timeout: float) -> tuple[int, str]:
        urls.append(url)
        return (200, GATED) if ":1994/" in url else (200, TOWER_OK)

    r = checks.remote_access(
        RA, resolve=lambda: "100.77.0.5", get=get, lan=lambda: [], probe=lambda h, p, t: False
    )
    assert r.severity == "ok"
    assert "http://100.77.0.5:4174/api/health" in urls
    assert not any("_stcore" in u for u in urls)


@pytest.mark.parametrize(
    ("body", "why"),
    [
        ("ok", "did not return JSON"),
        (json.dumps({"status": "degraded"}), "status is not ok"),
        ("[]", "status is not ok"),
    ],
)
def test_remote_tower_unhealthy_body_is_an_alert(body: str, why: str) -> None:
    r = _run(tower=(200, body))
    assert r.severity == "failed" and _keys(r) == {"remote_tower"}
    assert why in r.findings[0].message


def test_remote_exposed_on_lan() -> None:
    r = _run(lan=["192.168.1.20"], open_={"192.168.1.20:1994"})
    assert _keys(r) == {"remote_exposed"}
    assert "192.168.1.20:1994" in r.findings[0].message


def test_remote_no_tailscale_fails() -> None:
    def no_ts() -> str:
        raise net.NoTailscaleAddressError("none")

    r = checks.remote_access(RA, resolve=no_ts, lan=lambda: [], probe=lambda h, p, t: False)
    assert r.severity == "failed" and _keys(r) == {"remote_hermes", "remote_tower"}
    assert all("no Tailscale address" in f.message for f in r.findings)


def test_http_get_never_raises(tmp_path: Path) -> None:
    code, text = checks.http_get("http://127.0.0.1:9/nothing", 0.5)
    assert code == 0 and text
    assert checks.http_get("not a url", 0.5)[0] == 0


def test_http_get_status_codes() -> None:
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200 if self.path == "/ok" else 401)
            self.end_headers()
            self.wfile.write(b"fine")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        port = srv.server_address[1]
        assert checks.http_get(f"http://127.0.0.1:{port}/ok", 2) == (200, "fine")
        assert checks.http_get(f"http://127.0.0.1:{port}/no", 2)[0] == 401
        assert checks._port_open("127.0.0.1", port, 1)
    finally:
        srv.shutdown()
        srv.server_close()
    assert not checks._port_open("127.0.0.1", port, 0.5)


def test_shipped_config_remote_access_off_by_default() -> None:
    ra = load_routines(REPO / "config" / "routines.yaml").monitoring.remote_access
    assert ra.enabled is False and ra.dashboard_port == 1994 and ra.tower_port == 4174
    assert MonitoringSettings().remote_access.enabled is False
    assert RemoteAccessCheck.model_validate({"timeout": "7s"}).timeout.total_seconds() == 7
    with pytest.raises(ValueError):
        RemoteAccessCheck.model_validate({"bind": "0.0.0.0"})


def test_health_check_runs_remote_access_when_enabled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "routines.yaml"
    config.write_text(
        textwrap.dedent(
            """
            monitoring:
              gateway: {enabled: false}
              remote_access: {enabled: true, timeout: 1s}
            """
        )
    )
    db = str(tmp_path / "arc.db")
    with (
        mock.patch.object(net, "_tailscale_cli", return_value=None),
        mock.patch.object(net, "_run", return_value="inet 192.168.1.20 netmask"),
        mock.patch.object(checks, "_port_open", return_value=False),
    ):
        rc = main(["health", "check", "--db", db, "--config", str(config), "--no-slack",
                   "--now", "2026-09-28T09:00"])  # fmt: skip
    out = capsys.readouterr().out
    assert rc == 1
    assert "remote_access" in out and "no Tailscale address" in out
    open_keys = {a.key for a in alerts.AlertRepo(connect(db)).open_alerts()}
    assert {"remote_hermes", "remote_tower"} <= open_keys
    # --no-remote skips it and leaves the alerts open (not re-checked).
    main(["health", "check", "--db", db, "--config", str(config), "--no-slack", "--no-remote",
          "--now", "2026-09-28T09:01"])  # fmt: skip
    capsys.readouterr()
    assert {"remote_hermes", "remote_tower"} <= {
        a.key for a in alerts.AlertRepo(connect(db)).open_alerts()
    }
    # Back up: resolved with a line that says what is true now.
    ok = CheckResult("remote_access", "ok", "dashboard :1994 gated (basic), tower :4174 up")
    with mock.patch.object(checks, "remote_access", return_value=ok):
        main(["health", "check", "--db", db, "--config", str(config), "--no-slack",
              "--now", "2026-09-28T09:02"])  # fmt: skip
    out = capsys.readouterr().out
    assert "resolved: remote Hermes dashboard answering with basic auth again" in out
    assert "remote tower answering again" in out
    assert not {a.key for a in alerts.AlertRepo(connect(db)).open_alerts()} & {
        "remote_hermes",
        "remote_tower",
    }


def test_resolve_line_for_remote_exposed() -> None:
    from arc.monitoring.store import OpsAlert

    a = mock.Mock(spec=OpsAlert)
    a.key, a.message = "remote_exposed", "x"
    assert "no longer answering off the tailnet" in alerts._resolve_line(a, {})
    a.key = "remote_other"
    assert alerts._resolve_line(a, {}) == "remote_other cleared"
