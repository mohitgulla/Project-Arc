"""E9.4 step 7: the Sentinel's lens 7 (strategy) is info-only and defers to the Analyst.

The Sentinel skill is versioned in ``hermes/sentinel/`` and installed into the
``arc-sentinel`` profile by ``hermes/sentinel/install.sh``. These tests pin the
demotion so the two reviewers never file competing strategy cards.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SENTINEL = ROOT / "hermes" / "sentinel"
SKILL = SENTINEL / "skills" / "arc-sentinel" / "SKILL.md"


def _lens7() -> str:
    text = SKILL.read_text()
    m = re.search(r"^7\. \*\*Strategy.*?(?=^## )", text, flags=re.S | re.M)
    assert m, "lens 7 missing from the Sentinel skill"
    return m.group(0)


def test_lens7_is_info_only_no_action() -> None:
    lens = _lens7()
    assert "Analyst" in lens
    assert "`severity: info`" in lens
    assert "`action: no-action`" in lens
    assert "no `draft_card`" in lens
    assert "Never `new-card`" in lens


def test_lens7_states_the_ops_boundary() -> None:
    lens = _lens7()
    assert "decision the system makes" in lens
    assert "does what the spec says" in lens


def test_report_section_has_no_cards_for_strategy() -> None:
    text = SKILL.read_text()
    assert "*Strategy → Analyst*" in text
    assert "*Strategy & ideas*" not in text


def test_install_dry_run_copies_only_the_skill(tmp_path: Path) -> None:
    out = subprocess.run(
        ["bash", str(SENTINEL / "install.sh"), "--dry-run"],
        env={"ARC_SENTINEL_PROFILE_HOME": str(tmp_path / "p"), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert f"{tmp_path / 'p'}/skills/arc-sentinel/SKILL.md" in out
    assert "lessons.md" not in out
    assert "cron" not in out
    assert not (tmp_path / "p").exists()


SHARED = ROOT / "hermes" / "shared-skills"
SENTINEL_HELPERS = (
    "defuddle",
    "agent-reach",
    "code-review-and-quality",
    "security-and-hardening",
    "performance-optimization",
)


def test_install_dry_run_copies_pinned_helper_skills(tmp_path: Path) -> None:
    out = subprocess.run(
        ["bash", str(SENTINEL / "install.sh"), "--dry-run"],
        env={"ARC_SENTINEL_PROFILE_HOME": str(tmp_path / "p"), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for h in SENTINEL_HELPERS:
        assert (SHARED / h / "SKILL.md").is_file(), h
        assert f"{tmp_path / 'p'}/skills/{h}" in out, h
        assert h in SKILL.read_text(), h  # the skill names every helper it may load
    assert not (tmp_path / "p").exists()


def test_shared_helpers_have_no_dangling_relative_links() -> None:
    for md in SHARED.rglob("*.md"):
        text = md.read_text()
        assert "../references/" not in text, md
        for link in re.findall(r"\]\(((?:references/)?[a-z0-9-]+\.md)", text):
            assert (md.parent / link).is_file(), (md, link)
