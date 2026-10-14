"""``tests.hermes_env``: ~/.hermes/.env wins over stale inherited ALPACA_* values."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.hermes_env import refresh_alpaca_env

if TYPE_CHECKING:
    from pathlib import Path


def _env_file(tmp_path: Path, body: str) -> Path:
    p = tmp_path / ".env"
    p.write_text(body)
    return p


def test_file_value_replaces_stale_inherited_key(tmp_path: Path) -> None:
    f = _env_file(tmp_path, 'ALPACA_API_KEY=new\nexport ALPACA_SECRET_KEY="s2"\n')
    environ = {"ALPACA_API_KEY": "old", "ALPACA_SECRET_KEY": "s2"}
    assert refresh_alpaca_env(f, environ) == ["ALPACA_API_KEY"]
    assert environ == {"ALPACA_API_KEY": "new", "ALPACA_SECRET_KEY": "s2"}


def test_never_adds_a_key_that_was_not_exported(tmp_path: Path) -> None:
    # Adding one would turn a skipped integration test into a run (e.g. ALPACA_EXP_*).
    f = _env_file(tmp_path, "ALPACA_TEST_API_KEY=t\nALPACA_EXP_API_KEY=e\n")
    environ: dict[str, str] = {}
    assert refresh_alpaca_env(f, environ) == []
    assert environ == {}


def test_leaves_other_names_and_empty_values_alone(tmp_path: Path) -> None:
    f = _env_file(tmp_path, "SLACK_BOT_TOKEN=x\nARC_ENV=live\nALPACA_API_KEY=\n")
    environ = {"ALPACA_API_KEY": "kept", "ARC_ENV": "paper"}
    assert refresh_alpaca_env(f, environ) == []
    assert environ == {"ALPACA_API_KEY": "kept", "ARC_ENV": "paper"}


def test_missing_file_is_a_no_op(tmp_path: Path) -> None:
    environ = {"ALPACA_API_KEY": "old"}
    assert refresh_alpaca_env(tmp_path / "absent.env", environ) == []
    assert environ == {"ALPACA_API_KEY": "old"}
