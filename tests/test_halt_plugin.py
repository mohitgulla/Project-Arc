"""Tests for the Hermes gateway plugin ``hermes/plugins/arc-halt`` (E3.3)."""

from __future__ import annotations

import asyncio
import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

PLUGIN = Path(__file__).resolve().parent.parent / "hermes/plugins/arc-halt/__init__.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("arc_halt_plugin", PLUGIN)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


plugin = _load()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("!halt", ("halt", "")),
        ("!HALT vol spike", ("halt", "vol spike")),
        ("<@U0C4UH9TT5X> !halt now", ("halt", "now")),
        ("/halt", ("halt", "")),
        ("!resume", ("resume", "")),
        ("<@U0C4UH9TT5X> /resume", ("resume", "")),
        ("!halting", None),
        ("please !halt", None),
        ("!arc", None),
        ("", None),
        (None, None),
    ],
)
def test_parse(text: str | None, expected: tuple[str, str] | None) -> None:
    assert plugin.parse(text) == expected


class _Adapter:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str | None, dict[str, str] | None]] = []

    async def send(self, chat_id, content, reply_to=None, metadata=None):  # noqa: ANN001
        self.sent.append((chat_id, content, reply_to, metadata))


def _event(text: str, user: str = "U1", platform: str = "slack") -> SimpleNamespace:
    source = SimpleNamespace(
        platform=SimpleNamespace(value=platform), chat_id="C1", thread_id="111.1", user_id=user
    )
    return SimpleNamespace(text=text, source=source, message_id="222.2", user_id=user)


class _Gateway:
    """``gateway.adapters.get(platform)`` stand-in (SimpleNamespace platforms aren't hashable)."""

    def __init__(self, adapter: _Adapter) -> None:
        self.adapter = adapter
        self.adapters = self

    def get(self, platform: object) -> _Adapter:
        return self.adapter


def _gateway(adapter: _Adapter, event: SimpleNamespace) -> _Gateway:
    return _Gateway(adapter)


def test_hook_handles_halt_and_skips_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **kw):  # noqa: ANN001, ANN003
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="🛑 *HALT* ok\n", stderr="")

    monkeypatch.setattr(plugin.subprocess, "run", fake_run)
    ev, ad = _event("!halt vol", user="U_ANY"), _Adapter()
    result = asyncio.run(plugin.on_pre_gateway_dispatch(event=ev, gateway=_gateway(ad, ev)))
    assert result == {"action": "skip", "reason": "arc-halt handled !halt"}
    assert calls[0][1:] == ["slack-command", "--user", "U_ANY", "--text", "!halt vol"]
    assert ad.sent == [("C1", "🛑 *HALT* ok", "222.2", {"thread_id": "111.1"})]


def test_hook_ignores_other_text_and_platforms(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(plugin.subprocess, "run", lambda *a, **k: pytest.fail("ran"))
    ad = _Adapter()
    for ev in (_event("hello"), _event("!halt", platform="telegram")):
        assert (
            asyncio.run(plugin.on_pre_gateway_dispatch(event=ev, gateway=_gateway(ad, ev))) is None
        )
    assert asyncio.run(plugin.on_pre_gateway_dispatch(event=SimpleNamespace(text="!halt"))) is None
    assert ad.sent == []


def test_cli_failure_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        plugin.subprocess,
        "run",
        lambda argv, **k: subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom"),
    )
    assert "failed (exit 1)" in plugin.run_arc("halt", "", "U1")

    def raise_(*a: object, **k: object) -> None:
        raise subprocess.TimeoutExpired("arc", 20)

    monkeypatch.setattr(plugin.subprocess, "run", raise_)
    assert "use `arc halt` on the host" in plugin.run_arc("halt", "", "U1")


def test_register() -> None:
    hooks: dict[str, Any] = {}
    ctx = SimpleNamespace(register_hook=lambda name, cb: hooks.__setitem__(name, cb))
    plugin.register(ctx)
    assert hooks == {"pre_gateway_dispatch": plugin.on_pre_gateway_dispatch}


def test_arc_subprocess_gets_a_clean_python_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gateway's PYTHONPATH (Hermes's site-packages) must not reach Arc's venv.

    Regression: `!halt` from Slack exited 1 on a foreign pydantic_core import.
    """
    monkeypatch.setenv("PYTHONPATH", "/hermes/venv/lib/python3.14/site-packages")
    monkeypatch.setenv("PYTHONHOME", "/hermes/python")
    monkeypatch.setenv("VIRTUAL_ENV", "/hermes/venv")
    monkeypatch.setenv("ARC_ENV", "paper")
    seen: dict[str, Any] = {}

    def fake_run(cmd, **kw):  # noqa: ANN001, ANN003, ANN202
        seen["env"] = kw["env"]
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(plugin.subprocess, "run", fake_run)
    assert plugin.run_arc("halt", "test", "U0OWNER001") == "ok"
    env = seen["env"]
    assert not {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"} & set(env)
    assert env["ARC_ENV"] == "paper"
    assert env["PATH"].split(":")[0].endswith(".venv/bin")
