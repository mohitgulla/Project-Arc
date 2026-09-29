"""GET-only API routes for control tower v2, one module per page (E8.7, D35).

- :mod:`.meta`: ``/api/health`` and ``/api/meta`` (shell, as-of badge, settings)
- :mod:`.snapshot`: ``/api/snapshot`` (the full :class:`~arc.tower.data.TowerSnapshot`)

Pages add their own module (``overview.py`` E8.7a, ``trades.py`` E8.7b, ...).
Shared request plumbing lives in :mod:`.deps`.
"""
