"""hermes/hooks/arc-gate: hook script (unit) and a live run through Hermes (E3.2).

Unit tests run the script as a subprocess (the way Hermes runs it) and
in-process for the fail-closed import path.

The live tests (marker ``integration``) run the hook through the installed
Hermes hook runner (``hermes hooks test``), which uses the same code path as a
real ``pre_tool_call`` dispatch, against a throwaway ``HERMES_HOME`` under
``~/.hermes/cache/scratch`` (see the E3.2a note in that section). They skip when
``hermes`` is not on PATH. A module fixture fingerprints the installed launcher
around them and fails if they changed it.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
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
    from collections.abc import Iterator
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
        # wall-clock: the hook subprocess checks expiry against the real clock (now_et)
        expires_at=now_et() + dt.timedelta(minutes=10),
    )


def signed_token(p: Proposal) -> str:
    d = GateDecision(proposal_hash=proposal_hash(p), passed=True)
    # wall-clock: the token must be unexpired when the hook subprocess verifies it
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


def test_ledger_prunes_expired_ids(tmp_path: Path) -> None:
    """Recording an id drops ids whose token has expired; live and unparseable ids stay."""
    mod = load_hook_module()
    from arc.gate.token import parse_any

    arc1 = signed_token(proposal())
    exp = parse_any(arc1).expires_epoch
    arc2 = "arc2.x.y.1.2.3.99.sig.s1"  # unparseable arc2 id: kept
    ledger = tmp_path / "arc-gate" / "used_order_ids"
    mod._record_id(ledger, arc1, exp - 60)
    mod._record_id(ledger, arc2, exp - 60)
    mod._record_id(ledger, "not-a-token", exp - 60)
    assert mod._used_ids(ledger) == frozenset({arc1, arc2, "not-a-token"})
    mod._record_id(ledger, "new-id", exp + 1)  # the arc1 token is now expired
    assert mod._used_ids(ledger) == frozenset({arc2, "not-a-token", "new-id"})
    from tests.test_gate_band import band_token

    band = band_token()
    step_id = band + ".s2"
    band_exp = parse_any(band).expires_epoch
    mod._record_id(ledger, step_id, band_exp - 1)
    assert step_id in mod._used_ids(ledger)
    mod._record_id(ledger, "later", band_exp)  # expired arc2 step id is pruned too
    assert step_id not in mod._used_ids(ledger)


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

# Why the throwaway home lives under ~/.hermes (E3.2a). Hermes reads a HERMES_HOME
# outside its native root as a separate custom root with no runtime of its own
# (hermes_constants.get_default_hermes_root -> pm.environments.store_root /
# install_state_dir). The source-checkout launcher's boot (hermes_bootstrap ->
# hermes_cli.venv_sync.prepare_launch -> _finish_source_update) then provisions
# a whole PM store (~1 GB) into that home and runs
# source_completion.complete_source_checkout -> venv_sync.publish_launchers ->
# _launchers.ensure_install_launchers, which re-mints the host's shared
# <checkout>/.hermes/bin/hermes against <tmp>/hermes-home/tools/python... Every
# `hermes` then breaks once pytest deletes the tmp dir. A home under ~/.hermes
# reuses the host store and dependency selection read-only (like a profile), and
# HERMES_DISABLE_LAZY_INSTALLS=1 makes prepare_launch return before any sync or
# launcher publication. The hook runner is unchanged: same `hermes hooks test`
# pre_tool_call dispatch, same config, same hook script.
HERMES_NATIVE_ROOT = Path.home() / ".hermes"
HERMES_SCRATCH = HERMES_NATIVE_ROOT / "cache" / "scratch" / "arc-hook-tests"
_EXEC_TARGET = re.compile(r"""^\s*exec\s+(?:"([^"]+)"|'([^']+)'|(\S+))""", re.MULTILINE)

Fingerprint = list[tuple[str, str | None, str | None]]


def _launcher_chain(path: Path, max_hops: int = 6) -> list[Path]:
    """The launcher plus every file it hands off to: symlink targets and the
    ``exec /abs/path`` of shell forwarders (``~/.local/bin/hermes`` ->
    ``<checkout>/.hermes/bin/hermes``)."""
    chain: list[Path] = []
    current: Path | None = path
    while current is not None and len(chain) < max_hops and current not in chain:
        chain.append(current)
        nxt: Path | None = None
        if current.is_symlink():
            nxt = current.parent / os.readlink(current)
        elif current.is_file() and current.stat().st_size < 65536:
            text = current.read_bytes().decode("utf-8", "replace")
            m = _EXEC_TARGET.search(text) if text.startswith("#!") else None
            target = next((g for g in m.groups() if g), "") if m else ""
            if target.startswith("/") and Path(target).is_file():
                nxt = Path(target)
        current = nxt
    return chain


def launcher_fingerprint(path: Path) -> Fingerprint:
    """(path, symlink target, sha256) for every hop of the launcher chain."""
    out: Fingerprint = []
    for hop in _launcher_chain(path):
        link = os.readlink(hop) if hop.is_symlink() else None
        digest = hashlib.sha256(hop.read_bytes()).hexdigest() if hop.is_file() else None
        out.append((str(hop), link, digest))
    return out


def host_launchers() -> list[Path]:
    """Every entry point a user, cron or the gateway may run as `hermes`.

    ``which hermes`` differs by caller: a Hermes-spawned process sees the venv
    shim first, a login shell sees ``~/.local/bin/hermes`` -> the checkout's
    ``.hermes/bin/hermes`` (the file the bug re-minted)."""
    candidates = [HERMES, os.environ.get("HERMES_BIN"), str(Path.home() / ".local/bin/hermes")]
    out: list[Path] = []
    for c in candidates:
        if c and Path(c).exists() and Path(c) not in out:
            out.append(Path(c))
    return out


def host_fingerprint() -> Fingerprint:
    hops = {hop for p in host_launchers() for hop in launcher_fingerprint(p)}
    return sorted(hops, key=lambda h: (h[0], h[1] or "", h[2] or ""))


def assert_launcher_unchanged(before: Fingerprint, after: Fingerprint) -> None:
    if before != after:
        raise AssertionError(
            "the live Hermes tests mutated the host `hermes` launcher (E3.2a regression); "
            "repair it before anything else runs `hermes`.\n"
            f"before: {before}\nafter:  {after}"
        )


@pytest.fixture(scope="module")
def hermes_launcher_guard() -> Iterator[None]:
    """Fail loudly if running the live tests changed the installed launcher."""
    assert HERMES is not None
    which_before = shutil.which("hermes")
    before = host_fingerprint()
    yield
    assert shutil.which("hermes") == which_before, "`which hermes` changed during the live tests"
    assert_launcher_unchanged(before, host_fingerprint())


@pytest.fixture
def hermes_scratch(hermes_launcher_guard: None) -> Iterator[Path]:
    """A throwaway dir under ~/.hermes for the test HERMES_HOME (see above)."""
    if not HERMES_NATIVE_ROOT.is_dir():
        pytest.skip(f"no native Hermes root at {HERMES_NATIVE_ROOT}")
    HERMES_SCRATCH.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="live-", dir=HERMES_SCRATCH))
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _hermes_home(base: Path, command: str, timeout: int = 10) -> Path:
    home = base / "hermes-home"
    home.mkdir()
    assert home.resolve().is_relative_to(HERMES_NATIVE_ROOT.resolve())
    meta = yaml.safe_load((HOOK_DIR / "HOOK.yaml").read_text())
    entry = dict(meta["hermes_hooks"]["pre_tool_call"][0])
    entry.update(command=command, timeout=timeout)
    (home / "config.yaml").write_text(yaml.safe_dump({"hooks": {"pre_tool_call": [entry]}}))
    return home


def _hermes_hooks_test(home: Path, tool: str, args: dict, secret: str | None) -> dict | None:
    """Fire the configured pre_tool_call hooks via Hermes; return the parsed directive."""
    assert HERMES is not None
    pf = home / "payload.json"
    pf.write_text(json.dumps({"tool_name": tool, "args": args}))
    env = {k: v for k, v in os.environ.items() if k != "ARC_GATE_SECRET"}
    env["HERMES_HOME"] = str(home)
    # No dependency sync / source-update completion / launcher publication at boot.
    env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
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
def test_live_hermes_blocks_unsigned_order(hermes_scratch: Path) -> None:
    home = _hermes_home(hermes_scratch, _real_command())
    directive = _hermes_hooks_test(home, TOOL, order_args(), SECRET)
    assert directive is not None
    assert directive["action"] == "block"
    assert "no valid gate token" in directive["message"]


@pytest.mark.integration
@needs_hermes
def test_live_hermes_allows_signed_order(hermes_scratch: Path) -> None:
    home = _hermes_home(hermes_scratch, _real_command())
    assert _hermes_hooks_test(home, TOOL, order_args(signed_token(proposal())), SECRET) is None


@pytest.mark.integration
@needs_hermes
def test_live_hermes_blocks_stock_order_even_with_token(hermes_scratch: Path) -> None:
    home = _hermes_home(hermes_scratch, _real_command())
    args = {"symbol": "SPY", "side": "buy", "qty": "1", "client_order_id": "x"}
    directive = _hermes_hooks_test(home, "mcp__alpaca__place_stock_order", args, SECRET)
    assert directive is not None and directive["action"] == "block"


@pytest.mark.integration
@needs_hermes
def test_live_hermes_fails_closed_when_hook_cannot_start(
    tmp_path: Path, hermes_scratch: Path
) -> None:
    home = _hermes_home(hermes_scratch, f"{tmp_path}/no-such-python {HOOK}")
    directive = _hermes_hooks_test(home, TOOL, order_args(), SECRET)
    assert directive is not None
    assert directive["action"] == "block"
    assert "failed closed" in directive["message"]


@pytest.mark.integration
@needs_hermes
def test_live_hermes_fails_closed_on_timeout(tmp_path: Path, hermes_scratch: Path) -> None:
    slow = tmp_path / "slow.py"
    slow.write_text("import time\ntime.sleep(10)\n")
    home = _hermes_home(hermes_scratch, f"{sys.executable} {slow}", timeout=1)
    directive = _hermes_hooks_test(home, TOOL, order_args(signed_token(proposal())), SECRET)
    assert directive is not None
    assert directive["action"] == "block"
    assert "failed closed" in directive["message"]


# ---------------------------------------------------------------------------
# The launcher guard itself (E3.2a): a simulated re-mint must trip it
# ---------------------------------------------------------------------------


def _fake_launcher(tmp_path: Path) -> tuple[Path, Path, Path]:
    """symlink -> forwarder (``exec <target>``) -> target, like ~/.local/bin/hermes."""
    target = tmp_path / "checkout" / "hermes"
    target.parent.mkdir()
    target.write_text("#!/bin/sh\nexec /store/python3 -I -c 'boot' \"$@\"\n")
    forwarder = tmp_path / "local-bin-hermes"
    forwarder.write_text(f'#!/bin/sh\nexec {target} "$@"\n')
    link = tmp_path / "hermes"
    link.symlink_to(forwarder)
    return link, forwarder, target


def test_launcher_fingerprint_follows_symlink_and_exec_forwarder(tmp_path: Path) -> None:
    link, forwarder, target = _fake_launcher(tmp_path)
    fp = launcher_fingerprint(link)
    assert [hop[0] for hop in fp] == [str(link), str(forwarder), str(target)]
    assert fp[0][1] == str(forwarder)
    assert fp[0][2] is not None and fp[1][2] is not None and fp[2][2] is not None


def test_launcher_guard_fails_when_the_exec_target_is_reminted(tmp_path: Path) -> None:
    """The E3.2a bug: the shared launcher re-minted against a pytest tmp store."""
    link, _, target = _fake_launcher(tmp_path)
    before = launcher_fingerprint(link)
    assert_launcher_unchanged(before, launcher_fingerprint(link))
    target.write_text("#!/bin/sh\nexec /tmp/pytest-of-x/hermes-home/tools/python3 -I -c 'boot'\n")
    with pytest.raises(AssertionError, match="mutated the host `hermes` launcher"):
        assert_launcher_unchanged(before, launcher_fingerprint(link))


def test_launcher_guard_fails_when_the_symlink_is_repointed(tmp_path: Path) -> None:
    link, _, target = _fake_launcher(tmp_path)
    before = launcher_fingerprint(link)
    link.unlink()
    link.symlink_to(target)
    with pytest.raises(AssertionError, match="mutated"):
        assert_launcher_unchanged(before, launcher_fingerprint(link))


@needs_hermes
def test_real_launcher_chain_is_fingerprinted() -> None:
    assert HERMES is not None
    fp = launcher_fingerprint(Path(HERMES))
    assert fp[0][0] == HERMES
    assert all(digest is not None for _, link, digest in fp if link is None)
    host = host_fingerprint()
    assert {hop[0] for hop in fp} <= {hop[0] for hop in host}
