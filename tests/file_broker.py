"""A sqlite-backed fake :class:`~arc.broker.base.BrokerAdapter` shared across processes (E11.2).

The kill -9 tests run a real ladder in a child process and re-attach from the
test process; both see the same broker state through this file. ``mode`` (a
row in ``config``) decides what a submitted order does:

- ``work``: stays ``new`` until cancelled (then ``canceled``);
- ``filled`` / ``partial``: set by the test with :meth:`FileBroker.set_status`.

Every call is logged in ``calls`` so tests can assert, e.g., that no submit
happened during a re-attach.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from typing import TYPE_CHECKING

from arc.broker.base import BrokerOrderStatus

if TYPE_CHECKING:
    from pathlib import Path

    from arc.broker.base import MlegOrder

_DDL = """
CREATE TABLE IF NOT EXISTS orders (
    bid TEXT PRIMARY KEY, coid TEXT UNIQUE, status TEXT, filled_qty INTEGER,
    price TEXT, side TEXT
);
CREATE TABLE IF NOT EXISTS calls (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, arg TEXT);
CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT);
"""


class FileBroker:
    supports_mleg = True

    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        with self._db() as db:
            db.executescript(_DDL)

    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.execute("PRAGMA journal_mode = WAL")
        return db

    def _log(self, db: sqlite3.Connection, name: str, arg: str) -> None:
        db.execute("INSERT INTO calls (name, arg) VALUES (?, ?)", (name, arg))

    # -- test controls ---------------------------------------------------------

    def set_status(
        self, bid: str, status: str, *, filled: int = 0, price: str | None = None
    ) -> None:
        with self._db() as db:
            db.execute(
                "UPDATE orders SET status = ?, filled_qty = ?, price = ? WHERE bid = ?",
                (status, filled, price, bid),
            )

    def calls(self, name: str | None = None) -> list[tuple[str, str]]:
        with self._db() as db:
            rows = db.execute("SELECT name, arg FROM calls ORDER BY id").fetchall()
        return [(n, a) for n, a in rows if name is None or n == name]

    def order_ids(self) -> list[str]:
        with self._db() as db:
            return [r[0] for r in db.execute("SELECT bid FROM orders ORDER BY rowid")]

    # -- BrokerAdapter ---------------------------------------------------------

    def submit_mleg(self, order: MlegOrder) -> str:
        with self._db() as db:
            n = db.execute("SELECT count(*) FROM orders").fetchone()[0]
            bid = f"fb-{n}"
            db.execute(
                "INSERT INTO orders VALUES (?, ?, 'new', 0, NULL, NULL)",
                (bid, order.client_order_id),
            )
            self._log(db, "submit", order.client_order_id)
        return bid

    def cancel(self, broker_order_id: str) -> None:
        with self._db() as db:
            self._log(db, "cancel", broker_order_id)
            db.execute(
                """UPDATE orders SET status = 'canceled'
                   WHERE bid = ? AND status IN ('new', 'accepted', 'partially_filled')""",
                (broker_order_id,),
            )

    def _status(self, row: tuple[object, ...] | None) -> BrokerOrderStatus | None:
        if row is None:
            return None
        bid, coid, status, filled, price, side = row
        return BrokerOrderStatus(
            broker_order_id=str(bid),
            client_order_id=str(coid),
            status=str(status),
            filled_qty=Decimal(int(filled or 0)),  # type: ignore[call-overload]
            filled_avg_price=Decimal(str(price)) if price is not None else None,
            side=str(side) if side is not None else None,
        )

    def order_status(self, broker_order_id: str) -> BrokerOrderStatus:
        with self._db() as db:
            self._log(db, "status", broker_order_id)
            row = db.execute("SELECT * FROM orders WHERE bid = ?", (broker_order_id,)).fetchone()
        st = self._status(row)
        if st is None:
            msg = f"no order {broker_order_id}"
            raise LookupError(msg)
        return st

    def order_status_by_client_id(self, client_order_id: str) -> BrokerOrderStatus | None:
        with self._db() as db:
            self._log(db, "lookup", client_order_id)
            row = db.execute("SELECT * FROM orders WHERE coid = ?", (client_order_id,)).fetchone()
        return self._status(row)

    def cancel_by_client_id(self, client_order_id: str) -> None:
        st = self.order_status_by_client_id(client_order_id)
        if st is not None:
            self.cancel(st.broker_order_id)

    def account(self) -> object:  # unused by the ladder
        raise NotImplementedError

    def positions(self) -> list[object]:
        return []

    def fills(self, since: object) -> list[object]:
        return []
