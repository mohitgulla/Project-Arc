"""D26 control panel: runtime config overrides (owner-only, bounded, audited).

- :mod:`arc.control.registry`: every tunable key (pure).
- :mod:`arc.control.store`: the append-only ``config_changes`` log + pending confirms.
- :mod:`arc.control.effective`: defaults < YAML/env < DB overrides, for every entry point.
- :mod:`arc.control.service`: set / revert / confirm with owner and bounds checks.
"""

from __future__ import annotations

from arc.control.effective import cost_model, effective_routines, effective_settings, exit_config

__all__ = ["cost_model", "effective_routines", "effective_settings", "exit_config"]
