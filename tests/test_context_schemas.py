"""E5.6 / D27: the committed context schema registry matches the models."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from arc.cli import main
from arc.context.kinds import KINDS, SCHEMA_DIR

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_registry_matches_models() -> None:
    for name, spec in KINDS.items():
        path = SCHEMA_DIR / f"{name}.v{spec.schema_version}.json"
        assert path.exists(), f"run `arc context schemas --write` (missing {path.name})"
        committed = json.loads(path.read_text())
        assert committed == spec.model.model_json_schema(), (
            f"{name} model changed: bump schema_version and run `arc context schemas --write`"
        )


def test_no_orphan_schema_files() -> None:
    wanted = {f"{n}.v{s.schema_version}.json" for n, s in KINDS.items()}
    assert {p.name for p in SCHEMA_DIR.glob("*.json")} == wanted


def test_cli_check_and_write(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    reg = tmp_path / "reg"
    assert main(["context", "schemas", "--check", "--dir", str(reg)]) == 1
    assert "stale: note.v3.json" in capsys.readouterr().out
    assert main(["context", "schemas", "--write", "--dir", str(reg)]) == 0
    assert main(["context", "schemas", "--check", "--dir", str(reg)]) == 0
    # a hand-edited (drifted) schema and an orphan file are both caught
    (reg / "note.v3.json").write_text("{}\n")
    (reg / "gone.v1.json").write_text("{}\n")
    capsys.readouterr()
    assert main(["context", "schemas", "--check", "--dir", str(reg)]) == 1
    out = capsys.readouterr().out
    assert "stale: note.v3.json" in out and "orphan: gone.v1.json" in out
    assert main(["context", "schemas", "--write", "--dir", str(reg)]) == 0
    assert not (reg / "gone.v1.json").exists()
