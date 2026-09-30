"""Guard: tests inject ``now``; they never read the wall clock (E1.1b).

Main went red on 2026-09-29 because a test stamped rows with the real clock and its
TTL arithmetic depended on when the suite ran. This test parses every file under
``tests/`` and fails on any wall-clock call (``now_et()``, ``datetime.now()``,
``datetime.utcnow()``, ``datetime.today()``, ``date.today()``) that is not
allowlisted with a ``# wall-clock: <reason>`` comment on the same line or on the
comment line(s) directly above it. Only live integration tests (real broker/data,
a hook subprocess checking real expiry) should need the allowlist.

Parsing (not a text grep) means docstrings and comments that merely mention these
names do not trip the guard.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
ALLOW = re.compile(r"#\s*wall-clock:\s*\S")
_DATETIME_ATTRS = {"now", "utcnow", "today"}


def _is_wall_clock_call(node: ast.Call) -> str | None:
    """The call's display name if it reads the wall clock, else None."""
    func = node.func
    if isinstance(func, ast.Name) and func.id == "now_et":
        return "now_et()"
    if not isinstance(func, ast.Attribute):
        return None
    if func.attr == "now_et":
        return "now_et()"
    recv = func.value
    recv_name: str | None = None
    if isinstance(recv, ast.Name):
        recv_name = recv.id
    elif isinstance(recv, ast.Attribute):
        recv_name = recv.attr
    if recv_name == "datetime" and func.attr in _DATETIME_ATTRS:
        return f"datetime.{func.attr}()"
    if recv_name == "date" and func.attr == "today":
        return "date.today()"
    return None


def _allowlisted(lines: list[str], lineno: int) -> bool:
    """Same-line ``# wall-clock: <reason>``, or on the comment block directly above."""
    if ALLOW.search(lines[lineno - 1]):
        return True
    i = lineno - 2
    while i >= 0 and lines[i].lstrip().startswith("#"):
        if ALLOW.search(lines[i]):
            return True
        i -= 1
    return False


def find_violations(source: str, filename: str = "<src>") -> list[str]:
    """``file:line: call`` for every non-allowlisted wall-clock call in *source*."""
    tree = ast.parse(source, filename=filename)
    lines = source.splitlines()
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _is_wall_clock_call(node)
        if name is None:
            continue
        # A multi-line call may carry the comment on any of its lines.
        end = node.end_lineno or node.lineno
        if any(_allowlisted(lines, ln) for ln in range(node.lineno, end + 1)):
            continue
        out.append(f"{filename}:{node.lineno}: {name}")
    return sorted(out)


def test_no_wall_clock_reads_in_tests() -> None:
    violations: list[str] = []
    for path in sorted(TESTS_DIR.rglob("*.py")):
        rel = path.relative_to(TESTS_DIR.parent).as_posix()
        violations += find_violations(path.read_text(encoding="utf-8"), rel)
    assert not violations, (
        "Tests must inject a fixed `now`, never read the wall clock (AGENTS.md). "
        "Pin the clock, or add `# wall-clock: <reason>` for a live integration test:\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# The guard itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "src",
    [
        "now_et()\n",
        "cal.now_et()\n",
        "import datetime as dt\ndt.datetime.now(tz=None)\n",
        "from datetime import datetime\ndatetime.now()\n",
        "from datetime import datetime\ndatetime.utcnow()\n",
        "import datetime as dt\ndt.datetime.today()\n",
        "import datetime as dt\ndt.date.today()\n",
        "from datetime import date\ndate.today()\n",
        "x = 1  # wall-clock:\nnow_et()\n",  # empty reason does not count
        "# wall-clock: reason\n\nnow_et()\n",  # a blank line breaks the comment block
    ],
)
def test_guard_flags_wall_clock_calls(src: str) -> None:
    assert len(find_violations(src)) == 1


@pytest.mark.parametrize(
    "src",
    [
        "now_et()  # wall-clock: live broker RTH check\n",
        "# wall-clock: live quotes\nnow_et()\n",
        "# wall-clock: live quotes\n# (more context)\nx = now_et()\n",
        "f(\n    now=now_et(),  # wall-clock: hook subprocess\n)\n",
        '"""Mentions now_et() and datetime.now( in a docstring."""\n',
        "# now_et() in a comment\n",
        "NOW = 1\nf(now=NOW)\n",
        "import datetime as dt\ndt.datetime(2026, 1, 1)\n",
    ],
)
def test_guard_allows_pinned_or_annotated(src: str) -> None:
    assert find_violations(src) == []
