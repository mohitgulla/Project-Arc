"""Pre-D128 shapes for tests pinned on them: the old ``margin`` profile and control menu.

D128 made the shipped ``margin`` profile (now the default) a superset of ``cash_debit``
with the D97/D118 sleeves. Tests written against the old shape (one global 30-45 window,
credit strategies only) keep exercising that back-compat path through this file, which
is the shipped ``config/account_profiles.yaml`` with ``margin`` rewritten to its old shape.
"""

from __future__ import annotations

import functools
import tempfile
from pathlib import Path
from typing import Any

import yaml

from arc.config import ArcSettings

REPO = Path(__file__).resolve().parent.parent

LEGACY_MARGIN_STRATEGIES = {
    "bullish": ["bull_put"],
    "bearish": ["bear_call"],
    "neutral": ["iron_condor"],
}


def write_legacy_margin(dest: Path) -> Path:
    """Write the shipped profiles file with ``margin`` in its pre-D128 shape to *dest*."""
    data = yaml.safe_load((REPO / "config" / "account_profiles.yaml").read_text())
    prof = data["profiles"]["margin"]
    prof.pop("sleeves", None)
    prof["dte_min"] = prof["dte_max"] = None
    prof["stance_strategies"] = {k: list(v) for k, v in LEGACY_MARGIN_STRATEGIES.items()}
    dest.write_text(yaml.safe_dump(data))
    return dest


@functools.cache
def legacy_margin_file() -> Path:
    """One legacy profiles file per test process (read-only once written)."""
    d = Path(tempfile.mkdtemp(prefix="arc-legacy-margin-"))
    return write_legacy_margin(d / "account_profiles.yaml")


def legacy_margin_settings(**kw: Any) -> ArcSettings:
    """``ArcSettings`` on the pre-D128 single-window, credit-only ``margin`` profile."""
    kw.setdefault("account_profiles_file", legacy_margin_file())
    return ArcSettings(_env_file=None, account_profile="margin", **kw)  # type: ignore[call-arg]


#: D26 override pinning the pre-D128 ``pipeline.menu_measure`` (``control``). The canned
#: fixture Quant replies name menu items by position on the control-ranked menu; under
#: the D128 default (``managed_net_ev_full``) the cut keeps a different top 5.
CONTROL_MENU: dict[tuple[str, ...], object] = {("pipeline", "menu_measure"): "control"}


def pin_control_menu(s: ArcSettings) -> ArcSettings:
    """Add the ``CONTROL_MENU`` exits override to *s* in place (keeps other overrides)."""
    ov = {k: dict(v) for k, v in s._yaml_overrides.items()}  # noqa: SLF001
    ov.setdefault("exits", {}).update(CONTROL_MENU)
    s._yaml_overrides = ov  # noqa: SLF001
    return s
