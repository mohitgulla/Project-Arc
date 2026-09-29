"""Apply dotted-path overrides to a loaded YAML mapping (D26 control panel).

The config loaders (``config/exits.yaml``, ``costs.yaml``, ``account_profiles.yaml``,
``routines.yaml``) take an optional ``overrides`` mapping of ``path -> value`` and
patch the raw YAML data *before* validation, so an override is validated by the
same pydantic model as the file itself. Pure: returns a new mapping.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["Overrides", "apply_overrides"]

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
