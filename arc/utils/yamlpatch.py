"""Apply dotted-path overrides to a loaded YAML mapping (D26 control panel).

The config loaders (``config/exits.yaml``, ``costs.yaml``, ``account_profiles.yaml``,
``routines.yaml``) take an optional ``overrides`` mapping of ``path -> value`` and
patch the raw YAML data *before* validation, so an override is validated by the
same pydantic model as the file itself.

E7.5a / D44 experiment overlays: :func:`deep_merge` lays a partial config file
over its base. ``arc backtest rank --experiment`` and the forward-experiment arms
in ``config/experiments/live/*.yaml`` share it, so both read the same format.
Pure: every function returns a new mapping.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["OVERLAY_HEADER", "Overrides", "apply_overrides", "deep_merge", "overlay_body"]

# Free-text header of an overlay file (what it tests); never part of the config.
OVERLAY_HEADER = "experiment"

# ("kinds", "long_call", "close_at_dte") -> 5
Overrides = dict[tuple[str, ...], Any]


def apply_overrides(
    data: Mapping[str, Any], overrides: Mapping[tuple[str, ...], Any] | None
) -> Any:
    """A deep copy of *data* with every ``path -> value`` in *overrides* set.

    Missing intermediate mappings are created; a non-mapping on the path raises
    ``ValueError`` (the override does not fit the file's shape).
    """
    out = copy.deepcopy(dict(data))
    for path, value in (overrides or {}).items():
        if not path:
            msg = "override path must not be empty"
            raise ValueError(msg)
        node: Any = out
        for part in path[:-1]:
            nxt = node.get(part) if isinstance(node, dict) else None
            if nxt is None:
                nxt = {}
                node[part] = nxt
            if not isinstance(nxt, dict):
                msg = f"override path {'.'.join(path)}: {part!r} is not a mapping"
                raise ValueError(msg)
            node = nxt
        if not isinstance(node, dict):  # pragma: no cover - guarded above
            msg = f"override path {'.'.join(path)} does not address a mapping"
            raise ValueError(msg)
        node[path[-1]] = copy.deepcopy(value)
    return out


def deep_merge(base: Mapping[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    """*over* laid on *base*: mappings merge key by key, any other value replaces.

    Lists and scalars in *over* replace the base value whole. Neither input is
    modified.
    """
    out = copy.deepcopy(dict(base))
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def overlay_body(data: Mapping[str, Any] | None) -> dict[str, Any]:
    """An overlay file's config part: *data* without its free-text ``experiment`` header."""
    out = dict(data or {})
    out.pop(OVERLAY_HEADER, None)
    return out
