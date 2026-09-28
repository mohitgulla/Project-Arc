"""hermes/hooks/arc-gate: hook script (unit) and a live run through Hermes (E3.2).

Unit tests run the script as a subprocess (the way Hermes runs it) and
in-process for the fail-closed import path.

The live tests (marker ``integration``) run the hook through the installed
Hermes hook runner (``hermes hooks test``), which uses the same code path as a
real ``pre_tool_call`` dispatch, against a throwaway ``HERMES_HOME``. They skip
when ``hermes`` is not on PATH.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
from decimal import Decimal as D
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

from arc.gate import issue_token, proposal_hash
from arc.models import GateDecision, Proposal, QuantMetrics, Sizing
from arc.structures import credit_vertical, format_occ
from arc.utils.calendar import now_et

if TYPE_CHECKING:
    from types import ModuleType

REPO = Path(__file__).resolve().parent.parent
HOOK_DIR = REPO / "hermes" / "hooks" / "arc-gate"
HOOK = HOOK_DIR / "hook.py"
SECRET = "g" * 32
EXP = dt.date(2027, 1, 15)
LP = format_occ("SPY", EXP, "put", 565)
SP = format_occ("SPY", EXP, "put", 570)
TOOL = "mcp__alpaca__place_option_order"


def proposal() -> Proposal:
    return Proposal(
        candidate_id="c",
        structure=credit_vertical(
            "put",
            "SPY",
            EXP,
            short_strike=570,
            short_premium="2.10",
            long_strike=565,
            long_premium="1.25",
            as_of=dt.date(2026, 12, 1),
        ),
        thesis="t",
        quant=QuantMetrics(pop=0.7, ev=D("1"), cost_bps=0.0),
        sizing=Sizing(contracts=1, notional=D("415"), pct_equity=0.01),
        expires_at=now_et() + dt.timedelta(minutes=10),
    )


def signed_token(p: Proposal) -> str:
    d = GateDecision(proposal_hash=proposal_hash(p), passed=True)
    tok = issue_token(d, p, secret=SECRET.encode(), now=now_et()).token
    assert tok is not None
    return tok


def order_args(token: str | None = None) -> dict[str, object]:
    args: dict[str, object] = {
        "qty": "1",
        "type": "limit",
        "time_in_force": "day",
        "limit_price": "-0.85",
        "legs": [
            {"symbol": SP, "side": "sell", "ratio_qty": "1"},
            {"symbol": LP, "side": "buy", "ratio_qty": "1"},
        ],
    }
    if token:
        args["client_order_id"] = token
    return args


def hook_env(secret: str | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "ARC_GATE_SECRET"}
    env["HOME"] = str(REPO / ".no-home")  # keep ~/.hermes/.env out of the unit tests
    if secret is not None:
        env["ARC_GATE_SECRET"] = secret
    return env


def run_hook(stdin: str, secret: str | None = SECRET) -> tuple[int, dict]:
    r = subprocess.run(
        [sys.executable, str(HOOK)],
        input=stdin,
        capture_output=True,
        text=True,
        env=hook_env(secret),
        timeout=60,
    )
    return r.returncode, json.loads(r.stdout)


def payload(tool: str, args: object) -> str:
    """The stdin JSON Hermes sends a shell hook (agent/shell_hooks._serialize_payload)."""
    return json.dumps(
        {
            "hook_event_name": "pre_tool_call",
            "tool_name": tool,
            "tool_input": args,
            "session_id": "s",
            "cwd": "/",
            "profile": "default",
            "extra": {},
        }
    )


def load_hook_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("arc_gate_hook", HOOK)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# HOOK.yaml
# ---------------------------------------------------------------------------


def test_hook_yaml_declares_fail_closed_pre_tool_call() -> None:
    meta = yaml.safe_load((HOOK_DIR / "HOOK.yaml").read_text())
    assert meta["name"] == "arc-gate"
    (entry,) = meta["hermes_hooks"]["pre_tool_call"]
    assert entry["fail_closed"] is True
    assert 1 <= entry["timeout"] <= 30
    assert entry["command"].endswith("hermes/hooks/arc-gate/hook.py")
    import re

    m = re.compile(entry["matcher"])
    for tool in (
        "terminal",
        "mcp__alpaca__place_option_order",
        "mcp__alpaca_paper__place_stock_order",
        "mcp__alpaca__place_crypto_order",
        "mcp__alpaca__replace_order_by_id",
        "mcp__alpaca__close_position",
        "mcp__alpaca__close_all_positions",
        "mcp__alpaca__exercise_options_position",
    ):
        assert m.fullmatch(tool), tool
    for tool in ("read_file", "mcp__alpaca__get_orders", "mcp__alpaca__cancel_order_by_id"):
        assert not m.fullmatch(tool), tool


# ---------------------------------------------------------------------------
# Script (subprocess)
# ---------------------------------------------------------------------------


def test_unsigned_order_blocked_exit_2() -> None:
    code, out = run_hook(payload(TOOL, order_args()))
    assert code == 2
    assert out["action"] == "block"
    assert "no valid gate token" in out["message"]


def test_signed_order_allowed() -> None:
    code, out = run_hook(payload(TOOL, order_args(signed_token(proposal()))))
    assert (code, out) == (0, {})


def test_signed_order_blocked_without_secret() -> None:
    code, out = run_hook(payload(TOOL, order_args(signed_token(proposal()))), secret=None)
    assert code == 2
    assert "ARC_GATE_SECRET" in out["message"]


def test_ordinary_terminal_command_fast_allowed() -> None:
    assert run_hook(payload("terminal", {"command": "ls"}), secret=None) == (0, {})


def test_arc_execute_without_token_blocked() -> None:
    code, out = run_hook(payload("terminal", {"command": "uv run arc execute"}))
    assert code == 2
    assert "--token" in out["message"]


@pytest.mark.parametrize("stdin", ["", "not json", "[]", "null"])
def test_garbage_stdin_blocked(stdin: str) -> None:
    code, out = run_hook(stdin)
    assert code == 2
    assert out["action"] == "block"


# ---------------------------------------------------------------------------
# Script (in-process): fail-closed paths
# ---------------------------------------------------------------------------


def _main(mod: ModuleType, stdin: str, monkeypatch: pytest.MonkeyPatch) -> tuple[int, dict]:
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    code = mod.main(stdin)
    return code, json.loads(buf.getvalue())


def test_policy_import_failure_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = load_hook_module()
    monkeypatch.setitem(sys.modules, "arc.gate.hook_policy", None)  # import -> ImportError
    code, out = _main(mod, payload(TOOL, order_args()), monkeypatch)
    assert code == 2
    assert "failing closed" in out["message"]


def test_secret_from_settings_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = load_hook_module()
    monkeypatch.delenv("ARC_GATE_SECRET", raising=False)
    monkeypatch.setattr(
        "arc.config.get_settings",
        lambda: __import__("arc.config", fromlist=["ArcSettings"]).ArcSettings(
            _env_file=None, gate_secret=SECRET
        ),
    )
    assert mod._secret() == SECRET.encode()


def test_secret_lookup_error_means_no_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = load_hook_module()
    monkeypatch.delenv("ARC_GATE_SECRET", raising=False)

    def boom() -> None:
        raise RuntimeError("no settings")

    monkeypatch.setattr("arc.config.get_settings", boom)
    assert mod._secret() is None


# ---------------------------------------------------------------------------
# Live: through the installed Hermes hook runner
# ---------------------------------------------------------------------------

HERMES = shutil.which("hermes")
needs_hermes = pytest.mark.skipif(HERMES is None, reason="hermes CLI not on PATH")


def _hermes_home(tmp_path: Path, command: str, timeout: int = 10) -> Path:
    home = tmp_path / "hermes-home"
    home.mkdir()
    meta = yaml.safe_load((HOOK_DIR / "HOOK.yaml").read_text())
    entry = dict(meta["hermes_hooks"]["pre_tool_call"][0])
    entry.update(command=command, timeout=timeout)
    (home / "config.yaml").write_text(yaml.safe_dump({"hooks": {"pre_tool_call": [entry]}}))
    return home


def _hermes_hooks_test(home: Path, tool: str, args: dict, secret: str | None) -> dict | None:
    """Fire the configured pre_tool_call hooks via Hermes; return the parsed directive."""
    pf = home / "payload.json"
    pf.write_text(json.dumps({"tool_name": tool, "args": args}))
    env = {k: v for k, v in os.environ.items() if k != "ARC_GATE_SECRET"}
    env["HERMES_HOME"] = str(home)
    if secret is not None:
        env["ARC_GATE_SECRET"] = secret
    r = subprocess.run(
        [HERMES, "hooks", "test", "pre_tool_call", "--for-tool", tool, "--payload-file", str(pf)],
        capture_output=True,
        text=True,
        env=env,
        cwd=home,
        timeout=180,
    )
    assert "Firing 1 hook(s)" in r.stdout, r.stdout + r.stderr
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("parsed (Hermes wire shape):"):
            return json.loads(line.split(":", 1)[1])
    assert "parsed: <none" in r.stdout, r.stdout
    return None


def _real_command() -> str:
    return f"{sys.executable} {HOOK}"


@pytest.mark.integration
@needs_hermes
def test_live_hermes_blocks_unsigned_order(tmp_path: Path) -> None:
    home = _hermes_home(tmp_path, _real_command())
    directive = _hermes_hooks_test(home, TOOL, order_args(), SECRET)
    assert directive is not None
    assert directive["action"] == "block"
    assert "no valid gate token" in directive["message"]


@pytest.mark.integration
@needs_hermes
def test_live_hermes_allows_signed_order(tmp_path: Path) -> None:
    home = _hermes_home(tmp_path, _real_command())
    assert _hermes_hooks_test(home, TOOL, order_args(signed_token(proposal())), SECRET) is None


@pytest.mark.integration
@needs_hermes
def test_live_hermes_blocks_stock_order_even_with_token(tmp_path: Path) -> None:
    home = _hermes_home(tmp_path, _real_command())
    args = {"symbol": "SPY", "side": "buy", "qty": "1", "client_order_id": "x"}
    directive = _hermes_hooks_test(home, "mcp__alpaca__place_stock_order", args, SECRET)
    assert directive is not None and directive["action"] == "block"


@pytest.mark.integration
@needs_hermes
def test_live_hermes_fails_closed_when_hook_cannot_start(tmp_path: Path) -> None:
    home = _hermes_home(tmp_path, f"{tmp_path}/no-such-python {HOOK}")
    directive = _hermes_hooks_test(home, TOOL, order_args(), SECRET)
    assert directive is not None
    assert directive["action"] == "block"
    assert "failed closed" in directive["message"]


@pytest.mark.integration
@needs_hermes
def test_live_hermes_fails_closed_on_timeout(tmp_path: Path) -> None:
    slow = tmp_path / "slow.py"
    slow.write_text("import time\ntime.sleep(10)\n")
    home = _hermes_home(tmp_path, f"{sys.executable} {slow}", timeout=1)
    directive = _hermes_hooks_test(home, TOOL, order_args(signed_token(proposal())), SECRET)
    assert directive is not None
    assert directive["action"] == "block"
    assert "failed closed" in directive["message"]
