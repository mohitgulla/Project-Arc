"""Read-only Streamlit control tower over Tailscale (PLAN §4 E8.3).

- :mod:`arc.tower.data`: SELECT-only views of the audit store (``mode=ro``)
- :mod:`arc.tower.net`: bind address = the Tailscale interface, never public
- :mod:`arc.tower.app`: the Streamlit page (no state-changing inputs)
- :mod:`arc.tower.cli`: ``arc tower serve|snapshot``
"""
