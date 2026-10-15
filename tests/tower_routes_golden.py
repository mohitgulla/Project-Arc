"""E15.6 (D69): every tower GET route on the tests/test_tower.py fixture store, as JSON.

Shared by the no-``?arm`` snapshot test (``tests/test_tower_arm_scope.py``). Its
golden ``tests/fixtures/tower_routes_golden.json`` was captured on main (1f6f2c5) before
E15.6 under pytest (the conftest settings matter): a throwaway test on the main tree
building the ``tests.test_tower`` ``db`` fixture app and writing
``json.dumps(dump(client, "a" * 64, golden_only=True), indent=1, sort_keys=True)``.

Normalised so the golden is stable: random row ids and wall-clock timestamps the
fixture writes are masked; the E15.6 additions (``scope``, ``shared_from_control``,
``/api/meta`` ``arms``) are stripped (their own tests pin them).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi.testclient import TestClient

E156_KEYS = ("scope", "shared_from_control")
#: Routes whose body is mostly derived from the YAML config (catalogues, the full D26 key
#: list, the job timeline, the source registry, universe settings). Left out of the
#: golden so a config default change elsewhere does not break it; the no-param test
#: still checks them against ``?arm=control`` and their key sets.
YAML_ROUTES = ("/api/meta", "/api/ops/config", "/api/ops/session", "/api/ops/sources",
               "/api/ops/universe")  # fmt: skip
_HEX_ID = re.compile(r"\b(os-)?[0-9a-f]{16}\b|\b[0-9a-f]{32}\b")
_VOLATILE = {"git_sha", "version", "db", "db_path", "created_at", "as_of"}


def _norm(v: Any, key: str = "") -> Any:
    if key in _VOLATILE:
        return "<volatile>"
    if isinstance(v, dict):
        return {k: _norm(x, k) for k, x in v.items()}
    if isinstance(v, list):
        return [_norm(x) for x in v]
    if isinstance(v, str):
        return _HEX_ID.sub("<id>", v)
    return v


def route_urls(client: TestClient, phash: str) -> list[str]:
    """Every GET path in the app's OpenAPI with its path params filled."""
    spec = client.get("/api/openapi.json").json()
    out = []
    for path in sorted(spec["paths"]):
        url = path.replace("{proposal_hash}", phash).replace("{experiment_id}", "XP-1")
        out.append(url.replace("{run_id}", "nope").replace("{entry_id}", "nope"))
    return out


def dump(
    client: TestClient, phash: str, *, strip_e156: bool = True, golden_only: bool = False
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for url in route_urls(client, phash):
        if golden_only and url in YAML_ROUTES:
            continue
        r = client.get(url)
        body = r.json()
        if strip_e156 and isinstance(body, dict):
            for k in E156_KEYS:
                body.pop(k, None)
            if url == "/api/meta":
                body.pop("arms", None)
        out[url] = {"status": r.status_code, "body": _norm(body)}
    return out
