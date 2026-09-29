"""Read-only control tower over Tailscale (PLAN §4 E8.3; v2 E8.7, D35).

- :mod:`arc.tower.data`: SELECT-only views of the audit store (``mode=ro``)
- :mod:`arc.tower.net`: bind address = the Tailscale interface, never public
- :mod:`arc.tower.app`: the Streamlit page (no state-changing inputs)
- :mod:`arc.tower.api`: v2 FastAPI app (GET-only JSON under ``/api`` + the built SPA)
- :mod:`arc.tower.routes`: v2 API routes; :mod:`arc.tower.schemas`: v2 response models
- :mod:`arc.tower.serve`: v2 uvicorn runner (same bind rules)
- :mod:`arc.tower.openapi`: dumps the v2 OpenAPI spec for the SPA's typed client
- :mod:`arc.tower.cli`: ``arc tower serve [--v2]|snapshot``
"""
