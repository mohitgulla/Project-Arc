"""Tests for the ``!arc`` control-panel commands in ``hermes/plugins/arc-status`` (E8.5, D26)."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

PLUGIN = Path(__file__).resolve().parent.parent / "hermes/plugins/arc-status/__init__.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("arc_status_plugin", PLUGIN)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


plugin = _load()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", None),
        ("all", None),
        ("t_379d5eaf", None),
        ("config", ["show"]),
        ("config risk", ["show", "risk"]),
        ("CONFIG max_alloc_pct extra", ["show", "max_alloc_pct"]),
        ("set max_alloc_pct 4%", ["set", "max_alloc_pct", "4%"]),
        (
            "set max_alloc_pct 4% -- trim risk",
            ["set", "max_alloc_pct", "4%", "--reason", "trim risk"],
        ),
        (
            'set routines.scalp.cadence "every 60m 09:00-16:00"',
            ["set", "routines.scalp.cadence", "every 60m 09:00-16:00"],
        ),
        ("set universe +NVDA -TSLA", ["set", "universe", "+NVDA", "-TSLA"]),
        ("set max_alloc_pct", ["usage", "set <key> <value> [-- reason]"]),
        ("profile margin", ["profile", "margin"]),
        ("profile", ["usage", "profile <name>"]),
        ("revert 12 -- oops", ["revert", "12", "--reason", "oops"]),
        ("confirm AB12CD", ["confirm", "AB12CD"]),
        ("confirm AB12CD -- ignored", ["confirm", "AB12CD"]),
        ("cancel AB12CD", ["cancel", "AB12CD"]),
        ("history", ["history"]),
        ("history max_alloc_pct", ["history", "max_alloc_pct"]),
        ("diff", ["diff"]),
        ('set a "unterminated', ["set", "a", '"unterminated']),
    ],
)
def test_parse_config(text: str, expected: list[str] | None) -> None:
    assert plugin.parse_config(text) == expected


def _completed(payload: dict, code: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout=json.dumps(payload), stderr="")


def _session(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    for k in ("PLATFORM", "USER_ID", "CHAT_ID", "THREAD_ID"):
        monkeypatch.delenv(f"HERMES_SESSION_{k}", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(f"HERMES_SESSION_{k}", v)
    monkeypatch.setattr(plugin, "_session", lambda name: __import__("os").environ.get(name, ""))


def test_actor_comes_from_session_not_text(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):  # noqa: ANN001, ANN003, ANN202
        calls.append(cmd)
        return _completed(
            {"text": "*[Control] Set*", "blocks": [], "result": {"outcome": "applied"}}
        )

    _session(monkeypatch, PLATFORM="slack", USER_ID="U0OWNER001", CHAT_ID="C1")
    monkeypatch.setattr(plugin.subprocess, "run", fake_run)
    out = asyncio.run(plugin._handle("set max_alloc_pct 4% -- actor U0FAKE"))
    assert out == "*[Control] Set*"
    cmd = calls[0]
    assert cmd[1:5] == ["config", "set", "max_alloc_pct", "4%"]
    assert cmd[cmd.index("--actor") + 1] == "U0OWNER001"
    assert cmd[cmd.index("--source") + 1] == "slack"
    assert cmd[cmd.index("--reason") + 1] == "actor U0FAKE"


def test_writes_need_a_slack_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plugin.subprocess, "run", lambda *a, **k: pytest.fail("ran CLI"))
    _session(monkeypatch)
    assert "Slack-only" in asyncio.run(plugin._handle("set max_alloc_pct 4%"))
    _session(monkeypatch, PLATFORM="telegram", USER_ID="123")
    assert "Slack-only" in asyncio.run(plugin._handle("confirm ABCDEF"))


def test_reads_work_without_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):  # noqa: ANN001, ANN003, ANN202
        calls.append(cmd)
        return _completed({"text": "config", "blocks": [], "result": {}})

    _session(monkeypatch)
    monkeypatch.setattr(plugin.subprocess, "run", fake_run)
    assert asyncio.run(plugin._handle("config")) == "config"
    assert calls[0][calls[0].index("--actor") + 1] == "anonymous"


def test_usage_and_cli_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    _session(monkeypatch, PLATFORM="slack", USER_ID="U1")
    assert asyncio.run(plugin._handle("set x")).startswith("Usage:")
    monkeypatch.setattr(
        plugin.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="boom", stderr="trace"),
    )
    assert "exit 1" in asyncio.run(plugin._handle("diff"))

    def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise subprocess.TimeoutExpired("arc", 60)

    monkeypatch.setattr(plugin.subprocess, "run", boom)
    assert "failed" in asyncio.run(plugin._handle("diff"))


class _Client:
    def __init__(self) -> None:
        self.posted: list[dict] = []
        self.updated: list[dict] = []

    async def chat_postMessage(self, **kw: Any) -> None:  # noqa: N802
        self.posted.append(kw)

    async def chat_update(self, **kw: Any) -> None:
        self.updated.append(kw)


def test_pending_posts_card_with_buttons(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _Client()
    monkeypatch.setattr(plugin.SLACK, "client", client)
    blocks = [{"type": "actions", "elements": []}]
    monkeypatch.setattr(
        plugin.subprocess,
        "run",
        lambda *a, **k: _completed(
            {"text": "confirm", "blocks": blocks, "result": {"outcome": "pending"}}
        ),
    )
    _session(monkeypatch, PLATFORM="slack", USER_ID="U1", CHAT_ID="C1", THREAD_ID="1.1")
    out = asyncio.run(plugin._handle("set max_alloc_pct 7%"))
    assert "posted above" in out
    assert client.posted == [
        {"channel": "C1", "thread_ts": "1.1", "text": "confirm", "blocks": blocks}
    ]
    # no client -> the text reply (which carries the code) is returned instead
    monkeypatch.setattr(plugin.SLACK, "client", None)
    assert asyncio.run(plugin._handle("set max_alloc_pct 7%")) == "confirm"


def _click(action_id: str = "arc_cfg_confirm", user: str = "U1") -> tuple[dict, dict]:
    body = {"user": {"id": user}, "container": {"channel_id": "C1", "message_ts": "9.9"}}
    return body, {"action_id": action_id, "value": "AB12CD"}


def test_parse_click() -> None:
    assert plugin.parse_click(*_click()) == ("confirm", "AB12CD", "U1", "C1", "9.9")
    assert plugin.parse_click(*_click("arc_cfg_cancel"))[0] == "cancel"
    assert plugin.parse_click(*_click("arc_approve")) is None
    assert plugin.parse_click({"user": {}}, {"action_id": "arc_cfg_confirm", "value": "X"}) is None
    assert plugin.parse_click({"user": {"id": "U"}}, {"action_id": "arc_cfg_confirm"}) is None


@pytest.mark.parametrize(("outcome", "updated"), [("applied", True), ("refused", False)])
def test_click_runs_cli_as_clicker(
    monkeypatch: pytest.MonkeyPatch, outcome: str, updated: bool
) -> None:
    client = _Client()
    monkeypatch.setattr(plugin.SLACK, "client", client)
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):  # noqa: ANN001, ANN003, ANN202
        calls.append(cmd)
        return _completed({"text": outcome, "blocks": [], "result": {"outcome": outcome}})

    monkeypatch.setattr(plugin.subprocess, "run", fake_run)
    acks: list[bool] = []

    async def ack() -> None:
        acks.append(True)

    asyncio.run(plugin.on_confirm_click(ack, *_click(user="U0CLICKER1")))
    assert acks == [True]
    assert calls[0][1:4] == ["config", "confirm", "AB12CD"]
    assert calls[0][calls[0].index("--actor") + 1] == "U0CLICKER1"
    assert bool(client.updated) is updated
    assert bool(client.posted) is not updated


def test_register() -> None:
    reg: dict[str, Any] = {"actions": [], "platform": []}

    class Ctx:
        def register_command(self, name: str, **kw: Any) -> None:
            reg["command"] = name

        def register_slack_action_handler(self, action_id: str, fn: Any) -> None:
            reg["actions"].append(action_id)

        def register_platform_handler(self, platform: str, fn: Any) -> None:
            reg["platform"].append(platform)

    plugin.register(Ctx())
    assert reg == {
        "command": "arc",
        "actions": ["arc_cfg_confirm", "arc_cfg_cancel"],
        "platform": ["slack"],
    }
    app = type("App", (), {"client": object()})()
    plugin.slack_handlers(app)
    assert plugin.SLACK.client is app.client
    plugin.SLACK.client = None


def test_action_ids_match_the_cards() -> None:
    from arc.control import cards

    assert set(plugin.CONFIRM_ACTIONS) == {cards.CONFIRM_ACTION, cards.CANCEL_ACTION}


# ---------------------------------------------------------------------------
# D32 order budget line (`!arc budget`, and under every digest)
# ---------------------------------------------------------------------------


def test_order_budget_line_from_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kw):  # noqa: ANN001, ANN202
        calls.append([str(c) for c in cmd])
        assert kw["timeout"] == plugin.BUDGET_TIMEOUT_S
        assert "PYTHONPATH" not in kw["env"]  # E8.5a: clean Python env for the arc CLI
        return _completed(
            {"used": 120, "limit": 200, "tier": "restrictive", "local": 118, "broker": 120}
        )

    monkeypatch.setattr(plugin.subprocess, "run", fake_run)
    line = plugin.order_budget_line()
    assert line == "orders today 120/200 (restrictive) · local 118 vs broker 120 (mismatch)"
    assert calls[0][1:] == ["budget", "status", "--json"]
    # `!arc budget` renders only that line
    assert plugin._render("budget") == line


def test_order_budget_line_survives_cli_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise subprocess.TimeoutExpired("arc", 60)

    monkeypatch.setattr(plugin.subprocess, "run", boom)
    assert plugin.order_budget_line() == "orders today: n/a (arc budget status failed)"
    monkeypatch.setattr(
        plugin.subprocess,
        "run",
        lambda *a, **k: _completed({"used": 3, "limit": 200, "tier": "normal"}),
    )
    assert plugin.order_budget_line() == "orders today 3/200 (normal)"


def test_arc_subprocess_gets_a_clean_python_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gateway's PYTHONPATH (Hermes's site-packages) must not reach Arc's venv."""
    monkeypatch.setenv("PYTHONPATH", "/hermes/venv/lib/python3.14/site-packages")
    monkeypatch.setenv("PYTHONHOME", "/hermes/python")
    monkeypatch.setenv("VIRTUAL_ENV", "/hermes/venv")
    monkeypatch.setenv("ARC_ENV", "paper")
    seen: dict[str, dict[str, str]] = {}

    def fake_run(cmd, **kw):  # noqa: ANN001, ANN003, ANN202
        seen["env"] = kw["env"]
        return _completed({"text": "ok", "blocks": None, "result": {"outcome": "ok"}})

    monkeypatch.setattr(plugin.subprocess, "run", fake_run)
    plugin.run_config(["config"], "U0OWNER001")
    env = seen["env"]
    assert not {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"} & set(env)
    assert env["ARC_ENV"] == "paper"  # non-Python settings still pass through
    assert env["PATH"].split(":")[0].endswith(".venv/bin")


def test_arc_cli_runs_under_a_polluted_gateway_env() -> None:
    """End to end: the real `arc config` exits 0 with a foreign PYTHONPATH set."""
    import subprocess as sp

    if not plugin._arc_bin().exists():
        pytest.skip("no .venv/bin/arc in this checkout")
    polluted = {**os.environ, "PYTHONPATH": "/nonexistent/site-packages"}
    out = sp.run(
        [str(plugin._arc_bin()), "config", "keys"],
        cwd=plugin.REPO_DIR,
        env=plugin._arc_env(polluted),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert out.returncode == 0, out.stderr[-500:]
