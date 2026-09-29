"""Dump the tower API's OpenAPI spec (E8.7): ``python -m arc.tower.openapi [out.json]``.

``make web-api`` writes it to ``web/openapi.json``; ``npm run gen:api`` turns that into
the typed client types in ``web/src/lib/api.gen.ts``. A test fails when the committed
spec drifts from the app, so the SPA's types always match the API.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

__all__ = ["main", "spec"]


def spec() -> dict[str, Any]:
    from arc.tower.api import create_app

    # The spec does not depend on the DB: routes are fixed at build time.
    return create_app(Path("/nonexistent/arc.db")).openapi()


def render() -> str:
    return json.dumps(spec(), indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    text = render()
    if args:
        Path(args[0]).write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
