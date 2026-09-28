"""Shared context store between agents (D16): typed, append-only, TTL'd.

Usage::

    from arc.context import ContextStore

    store = ContextStore(conn)
    store.write(kind="shortlist", subject="market", payload=out, produced_by="director",
                ttl="1 session", run_id=run_id)
    snap = store.snapshot(now, kinds=["candidate", "regime"])   # recorded; snap.id -> run
"""

from arc.context.kinds import KINDS, KindSpec, kind_spec, validate_payload
from arc.context.store import (
    ContextEntry,
    ContextSnapshot,
    ContextStore,
    EntryStatus,
    Supersede,
)
from arc.context.ttl import Ttl, parse_duration

__all__ = [
    "KINDS",
    "ContextEntry",
    "ContextSnapshot",
    "ContextStore",
    "EntryStatus",
    "KindSpec",
    "Supersede",
    "Ttl",
    "kind_spec",
    "parse_duration",
    "validate_payload",
]
