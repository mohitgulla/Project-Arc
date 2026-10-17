#!/usr/bin/env python3
"""E26.4 (D121): the pre-run gate of cron ``arc-analyst-verdict-check``.

Hermes runs a cron script with no arguments, so this wrapper calls
``arc_analyst.py gate --mode verdict`` (installed next to it by install.sh).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import arc_analyst  # noqa: E402

if __name__ == "__main__":
    sys.exit(arc_analyst.main(["gate", "--mode", "verdict"]))
