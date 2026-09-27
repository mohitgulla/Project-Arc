"""SQLite audit store: schema, migrations, event-sourced repositories.

Usage::

    from arc.store.db import connect
    from arc.store.migrate import migrate
    from arc.store.repos import OrderRepo

    conn = connect()        # data/arc.db (or ":memory:" for tests)
    migrate(conn)           # apply pending migrations
    orders = OrderRepo(conn)
"""

from arc.store.db import connect
from arc.store.migrate import migrate
from arc.store.order_state import IllegalTransitionError, validate_transition
from arc.store.repos import (
    ApprovalRepo,
    CandidateRepo,
    FillRepo,
    GateDecisionRepo,
    HaltRepo,
    OrderRepo,
    PnlSnapshotRepo,
    PositionsSnapshotRepo,
    ProposalRepo,
    TaxLotRepo,
)

__all__ = [
    "connect",
    "migrate",
    "validate_transition",
    "IllegalTransitionError",
    "ApprovalRepo",
    "CandidateRepo",
    "FillRepo",
    "GateDecisionRepo",
    "HaltRepo",
    "OrderRepo",
    "PnlSnapshotRepo",
    "PositionsSnapshotRepo",
    "ProposalRepo",
    "TaxLotRepo",
]
