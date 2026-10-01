"""Read-only control tower over Tailscale (PLAN §4 E8.3; v2 E8.7, D35).

- :mod:`arc.tower.data`: SELECT-only views of the audit store (``mode=ro``)
- :mod:`arc.tower.net`: bind address = the Tailscale interface, never public
- :mod:`arc.tower.api`: the FastAPI app (GET-only JSON under ``/api`` + the built SPA)
- :mod:`arc.tower.routes`: API routes; :mod:`arc.tower.schemas`: response models
- :mod:`arc.tower.serve`: uvicorn runner (Tailscale/loopback bind only)
- :mod:`arc.tower.openapi`: dumps the OpenAPI spec for the SPA's typed client
- :mod:`arc.tower.cli`: ``arc tower serve|snapshot``
"""
