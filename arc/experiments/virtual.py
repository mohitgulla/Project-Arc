"""The experiment arm's virtual account (E10.2, D44; owner decision 2026-10-03).

Alpaca cannot reset a paper account's balance, so an arm trades a *virtual*
sub-account inside its larger paper account and never sizes from the broker's
balance. The account is an append-only ledger (``virtual_ledger``), replayed
into :class:`VirtualState`:

* ``open`` (t0): virtual cash = control's equity at t0.
* ``legacy_hold`` / ``legacy_release``: control's open structures at t0 (the
  legacy book) reserve their max loss in the arm's buying power until each one
  closes on control.
* ``fill``: every broker fill of the arm's account, as a premium cash flow
  (sells +, buys -; fees included). Sale proceeds settle on the next session
  (T+1), which is what a cash account may spend before then (``settled``).
  Keyed on ``order:symbol:cumulative qty`` and booking only the increment over
  what the ledger holds for that leg (E10.2c: Alpaca's leg ``filled_at`` drifts).
* ``adjust``: a compensating row ``arc experiment repair-ledger`` appends for a
  fill booked twice before E10.2c (:mod:`arc.experiments.repair`).

Virtual equity = virtual cash + the market value of the arm's positions. In
**dedicated** mode the account was flat at t0, so every broker position is the
arm's own; in **shared** mode (D69, E15.2) the positions are the arm's own fill
ledger netted per contract and marked at the broker's price, never another arm's
legs, and the ledger is booked from the account's FILL / FEE / option-event
activities by :mod:`arc.experiments.attribution` (``act:`` / ``fee:`` / ``evt:``
refs). Equity moves only
with the arm's own P&L and is never re-synced to control. The excess broker
equity (paper $100k vs a $10k control) is never usable: every buying-power
figure handed to the trading code is ``min(broker, virtual - reserved)``.

:class:`VirtualBroker` wraps the arm's real broker so ``account()`` returns the
virtual account. Everything downstream (sizing, the gate's settled-cash rule,
the daily-loss baseline, reconcile's EOD ``pnl_snapshots``) is the unchanged
code reading that account: the gate never knows it is an arm.
"""

from __future__ import annotations

import datetime as _dt
import json
from collections import defaultdict
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field

from arc.broker.base import AccountInfo, BrokerPosition, Fill
from arc.context.ttl import from_db, to_db
from arc.utils.calendar import ET, next_session

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from arc.broker.base import BrokerAdapter

__all__ = [
    "MULTIPLIER",
    "LedgerRow",
    "SharedAccountError",
    "VirtualBroker",
    "VirtualState",
    "append_row",
    "arm_positions",
    "booked",
    "fill_amount",
    "fill_ref",
    "legacy_reservations",
    "open_account",
    "open_collateral",
    "order_symbol",
    "record_fills",
    "release_legacy",
    "replay",
    "virtual_account",
]

log = structlog.get_logger(__name__)

MULTIPLIER = Decimal(100)
LedgerKind = Literal["open", "legacy_hold", "legacy_release", "fill", "adjust", "fee", "event"]
#: rows that move cash and/or held contracts (E15.2 adds shared-mode fee + event)
_FLOW_KINDS = ("fill", "adjust", "fee", "event")


class LedgerRow(BaseModel):
    """One ``virtual_ledger`` row (append-only)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    arm_id: str
    kind: LedgerKind
    ref: str
    amount: Decimal
    settles_on: _dt.date | None = None
    at: _dt.datetime
    detail: dict[str, Any] = Field(default_factory=dict)


class VirtualState(BaseModel):
    """The arm's virtual account as of *as_of* (replayed from the ledger)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    arm_id: str
    as_of: _dt.date
    t0_equity: Decimal
    cash: Decimal = Field(..., description="t0 equity + every fill's cash flow")
    unsettled: Decimal = Field(..., ge=0, description="sale proceeds not settled by as_of")
    legacy_reserved: Decimal = Field(..., ge=0, description="max loss of open legacy structures")
    legacy_open: tuple[str, ...] = ()
    held: dict[str, Decimal] = Field(
        default_factory=dict, description="net contracts per OCC symbol from the arm's fills"
    )
    cash_start_of_day: Decimal = Field(..., description="cash from rows before as_of")
    held_start_of_day: dict[str, Decimal] = Field(default_factory=dict)

    @property
    def spendable(self) -> Decimal:
        """Cash the arm may commit: virtual cash less the legacy reservation."""
        return max(self.cash - self.legacy_reserved, Decimal(0))

    @property
    def settled(self) -> Decimal:
        """Spendable cash a cash account may use now (unsettled proceeds excluded)."""
        return max(self.cash - self.unsettled - self.legacy_reserved, Decimal(0))


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


def _insert(conn: sqlite3.Connection, row: LedgerRow) -> bool:
    cur = conn.execute(
        """INSERT OR IGNORE INTO virtual_ledger
           (arm_id, kind, ref, amount, settles_on, at, detail)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            row.arm_id,
            row.kind,
            row.ref,
            str(row.amount),
            row.settles_on.isoformat() if row.settles_on else None,
            to_db(row.at),
            json.dumps(row.detail, sort_keys=True, default=str),
        ),
    )
    return cur.rowcount > 0


def append_row(conn: sqlite3.Connection, row: LedgerRow) -> bool:
    """Append *row* unless its ``(arm_id, kind, ref)`` is already booked (no commit)."""
    return _insert(conn, row)


def rows(conn: sqlite3.Connection, arm_id: str) -> list[LedgerRow]:
    out: list[LedgerRow] = []
    for r in conn.execute(
        """SELECT arm_id, kind, ref, amount, settles_on, at, detail FROM virtual_ledger
           WHERE arm_id = ? ORDER BY id""",
        (arm_id,),
    ):
        out.append(
            LedgerRow(
                arm_id=r[0],
                kind=r[1],
                ref=r[2],
                amount=Decimal(r[3]),
                settles_on=_dt.date.fromisoformat(r[4]) if r[4] else None,
                at=from_db(r[5]),
                detail=json.loads(r[6] or "{}"),
            )
        )
    return out


def open_account(
    conn: sqlite3.Connection,
    arm_id: str,
    *,
    t0_equity: Decimal,
    legacy: dict[str, Decimal],
    at: _dt.datetime,
) -> None:
    """t0: open the virtual account at control's equity and hold the legacy book."""
    if t0_equity <= 0:
        msg = f"t0 equity must be positive, got {t0_equity}"
        raise ValueError(msg)
    with conn:
        _insert(conn, LedgerRow(arm_id=arm_id, kind="open", ref="t0", amount=t0_equity, at=at))
        for sid, reserve in sorted(legacy.items()):
            _insert(
                conn,
                LedgerRow(arm_id=arm_id, kind="legacy_hold", ref=sid, amount=reserve, at=at),
            )


def release_legacy(
    conn: sqlite3.Connection, arm_id: str, closed: Iterable[str], *, at: _dt.datetime
) -> list[str]:
    """Release the hold of every legacy structure in *closed* (idempotent)."""
    held = {r.ref for r in rows(conn, arm_id) if r.kind == "legacy_hold"}
    out: list[str] = []
    with conn:
        for sid in sorted(set(closed) & held):
            if _insert(
                conn,
                LedgerRow(arm_id=arm_id, kind="legacy_release", ref=sid, amount=Decimal(0), at=at),
            ):
                out.append(sid)
    return out


def fill_amount(fill: Fill, *, fee_per_contract: Decimal = Decimal(0)) -> Decimal:
    """Premium cash flow of one leg fill: sells +, buys -, less fees."""
    gross = fill.price * fill.qty * MULTIPLIER
    sign = Decimal(1) if fill.side.lower().startswith("sell") else Decimal(-1)
    return sign * gross - fee_per_contract * fill.qty


def _qty_text(q: Decimal) -> str:
    """Canonical text of a contract count (``1``, ``1.0`` and ``1E0`` are one key)."""
    return f"{abs(q).normalize():f}"


def fill_ref(broker_order_id: str, symbol: str, cum_qty: Decimal) -> str:
    """The ledger ref of a leg fill: stable fields only (E10.2c).

    ``<broker_order_id>:<symbol>:<cumulative filled qty>``. Alpaca's mleg leg
    ``filled_at`` drifts by microseconds between calls, so neither the time nor the
    (average) price may be in the key. Shared mode (E15.2) books FILL activities by
    activity id instead; this key stays the dedicated-mode one.
    """
    return f"{broker_order_id}:{symbol}:{_qty_text(cum_qty)}"


def order_symbol(ref: str) -> tuple[str, str]:
    """``(broker_order_id, symbol)`` of a ``fill`` / ``adjust`` ref, any format.

    Pre-E10.2c refs are ``order:symbol:<filled_at iso>:qty:price``; the order id
    (a UUID) and the OCC symbol never contain a colon, so the prefix is shared.
    """
    order, symbol, *_ = ref.split(":", 2)
    return order, symbol


def booked(ledger: Iterable[LedgerRow]) -> dict[tuple[str, str], tuple[Decimal, Decimal]]:
    """``(order, symbol)`` -> (signed contracts, cash) already booked by fill + adjust rows."""
    out: dict[tuple[str, str], tuple[Decimal, Decimal]] = {}
    for r in ledger:
        if r.kind not in ("fill", "adjust"):
            continue
        key = order_symbol(r.ref)
        qty, cash = out.get(key, (Decimal(0), Decimal(0)))
        out[key] = (qty + Decimal(str(r.detail.get("qty", "0"))), cash + r.amount)
    return out


def record_fills(
    conn: sqlite3.Connection,
    arm_id: str,
    fills: Sequence[Fill],
    *,
    fee_per_contract: Decimal = Decimal(0),
) -> int:
    """Book each broker leg fill's *increment* (idempotent). Returns how many rows were new.

    A :class:`Fill` carries the leg's cumulative ``filled_qty`` and average price.
    The row books what the ledger does not hold yet for ``(order, symbol)``: the
    contracts above those already booked, and the cumulative cash flow less the
    cash already booked, so a partial then full fill totals the full fill exactly.
    A re-fetch of a fill already booked (same or lower cumulative qty, any
    ``filled_at`` drift) books nothing.
    """
    n = 0
    with conn:
        have = booked(rows(conn, arm_id))
        for f in sorted(fills, key=lambda x: (x.filled_at, x.broker_order_id, x.symbol)):
            key = (f.broker_order_id, f.symbol)
            sign = Decimal(-1) if f.side.lower().startswith("sell") else Decimal(1)
            cum_qty = sign * abs(f.qty)
            cum_amount = fill_amount(f, fee_per_contract=fee_per_contract)
            qty0, cash0 = have.get(key, (Decimal(0), Decimal(0)))
            if abs(cum_qty) <= abs(qty0):
                continue
            amount = cum_amount - cash0
            day = f.filled_at.astimezone(ET).date()
            settles = next_session(day) if amount > 0 else day
            new = _insert(
                conn,
                LedgerRow(
                    arm_id=arm_id,
                    kind="fill",
                    ref=fill_ref(f.broker_order_id, f.symbol, f.qty),
                    amount=amount,
                    settles_on=settles,
                    at=f.filled_at,
                    detail={
                        "symbol": f.symbol,
                        "qty": str(cum_qty - qty0),
                        "price": str(f.price),
                        "cum_qty": str(cum_qty),
                        "cum_amount": str(cum_amount),
                    },
                ),
            )
            if new:
                have[key] = (cum_qty, cum_amount)
                n += 1
    return n


def replay(ledger: Sequence[LedgerRow], *, as_of: _dt.date) -> VirtualState:
    """Fold the ledger into the account state on ET day *as_of*.

    Pure: the same rows give the same state (the "replaying the ledger from fills
    reproduces the virtual state" acceptance).
    """
    opens = [r for r in ledger if r.kind == "open"]
    if not opens:
        msg = "virtual ledger has no t0 'open' row"
        raise ValueError(msg)
    arm_id = opens[0].arm_id
    t0_equity = opens[0].amount
    cash = t0_equity
    cash_sod = t0_equity
    unsettled = Decimal(0)
    holds: dict[str, Decimal] = {}
    released: set[str] = set()
    held: dict[str, Decimal] = defaultdict(Decimal)
    held_sod: dict[str, Decimal] = defaultdict(Decimal)
    for r in ledger:
        if r.kind == "legacy_hold":
            holds[r.ref] = r.amount
        elif r.kind == "legacy_release":
            released.add(r.ref)
        elif r.kind in _FLOW_KINDS:
            day = r.at.astimezone(ET).date()
            if day > as_of:
                continue
            cash += r.amount
            sym, qty = str(r.detail.get("symbol", "")), Decimal(str(r.detail.get("qty", "0")))
            if sym:
                held[sym] += qty
            if day < as_of:
                cash_sod += r.amount
                if sym:
                    held_sod[sym] += qty
            pending = r.settles_on is not None and r.settles_on > as_of
            if pending and (r.amount > 0 or r.kind == "adjust"):
                # an adjust nets out the unsettled proceeds of the fill it compensates
                unsettled += r.amount
    unsettled = max(unsettled, Decimal(0))
    open_legacy = tuple(sorted(set(holds) - released))
    return VirtualState(
        arm_id=arm_id,
        as_of=as_of,
        t0_equity=t0_equity,
        cash=cash,
        unsettled=unsettled,
        legacy_reserved=sum((holds[s] for s in open_legacy), Decimal(0)),
        legacy_open=open_legacy,
        held={k: v for k, v in sorted(held.items()) if v},
        cash_start_of_day=cash_sod,
        held_start_of_day={k: v for k, v in sorted(held_sod.items()) if v},
    )


# ---------------------------------------------------------------------------
# Legacy book
# ---------------------------------------------------------------------------


def legacy_reservations(control: sqlite3.Connection) -> dict[str, Decimal]:
    """Control's open structures (the legacy book) -> max loss in dollars to reserve.

    A structure whose max loss is unbounded or unreadable reserves its entry debit
    floor of 0 is never assumed: it raises, because the arm must not start against
    a legacy book it cannot bound.
    """
    from arc.models import Structure
    from arc.structures.analytics import max_gain_loss

    out: dict[str, Decimal] = {}
    for r in control.execute(
        "SELECT id, structure_json, contracts FROM open_structures WHERE status = 'open'"
    ):
        st = Structure.model_validate_json(r[1])
        _, loss = max_gain_loss(st.legs)
        if loss is None:
            msg = f"legacy structure {r[0]} has unbounded max loss; flatten it before t0"
            raise ValueError(msg)
        out[str(r[0])] = (loss * int(r[2])).quantize(Decimal("0.01"))
    return out


def closed_legacy(control: sqlite3.Connection, ids: Iterable[str]) -> list[str]:
    """Which of *ids* are no longer open on control."""
    ids = sorted(set(ids))
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    open_ = {
        r[0]
        for r in control.execute(
            f"SELECT id FROM open_structures WHERE status = 'open' AND id IN ({marks})",  # noqa: S608
            ids,
        )
    }
    return [i for i in ids if i not in open_]


# ---------------------------------------------------------------------------
# Collateral of the arm's own open structures (D129)
# ---------------------------------------------------------------------------


def open_collateral(arm: sqlite3.Connection) -> Decimal:
    """Options buying power the arm's own open structures hold (D129).

    Alpaca reduces ``options_buying_power`` by a defined-risk structure's max loss
    while the arm's virtual cash only moved by the premium. The difference is what
    the virtual account must still reserve, per structure:
    ``contracts × (max_loss_unit − entry_net × 100)`` where ``max_loss_unit`` is
    ``Structure.max_loss`` (dollars per unit) and ``entry_net`` the per-share fill
    (+ debit / − credit). A debit spread (max loss = the debit) reserves 0, as the
    debit already left cash; a credit spread reserves width × 100 (its
    (width − credit) × 100 max loss plus the credit that came in). Structures whose
    max loss is unknown are skipped; negatives are floored at 0.
    """
    from arc.models import Structure

    total = Decimal(0)
    for r in arm.execute(
        "SELECT structure_json, contracts, entry_net FROM open_structures WHERE status = 'open'"
    ):
        st = Structure.model_validate_json(r[0])
        if st.max_loss is None:
            continue
        per_unit = st.max_loss - Decimal(str(r[2])) * MULTIPLIER
        total += max(per_unit, Decimal(0)) * int(r[1])
    return total.quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# The account the trading code sees
# ---------------------------------------------------------------------------


def _signed_value(p: BrokerPosition) -> Decimal | None:
    if p.market_value is None:
        return None
    mv = p.market_value
    # Alpaca reports short market value negative; normalise a positive short value.
    return -abs(mv) if p.side == "short" else abs(mv)


def virtual_account(
    state: VirtualState,
    broker: AccountInfo,
    positions: Sequence[BrokerPosition],
    *,
    cash_settlement: bool,
    collateral: Decimal = Decimal(0),
) -> AccountInfo:
    """The arm's account: virtual equity and buying power capped by both books.

    ``cash_settlement`` (control's profile is a cash account): non-marginable
    buying power is the *settled* virtual cash, so the gate's ``cash_settled`` rule
    (via :func:`arc.pipeline.market.settled_cash`) rejects opens that would spend
    unsettled proceeds.

    ``collateral`` (D129, :func:`open_collateral`): options buying power the arm's
    own open structures hold beyond the premium that left cash. It is taken off
    ``buying_power`` and ``options_buying_power`` (never below 0), so a margin arm's
    gate rule and sizing fit see what Alpaca would report for the arm alone.
    """
    marks = [_signed_value(p) for p in positions if p.asset_class == "us_option"]
    equity = state.cash + sum((m for m in marks if m is not None), Decimal(0))
    spend = state.spendable
    free = max(spend - collateral, Decimal(0))
    nmbp = state.settled if cash_settlement else spend
    last = state.cash_start_of_day
    by_symbol = {p.symbol: p for p in positions}
    for sym, qty in state.held_start_of_day.items():
        p = by_symbol.get(sym)
        px = p.lastday_price if p is not None and p.lastday_price is not None else None
        if px is not None:
            last += qty * px * MULTIPLIER
    return AccountInfo(
        account_id=broker.account_id,
        equity=equity,
        buying_power=min(broker.buying_power, free),
        cash=spend,
        currency=broker.currency,
        options_buying_power=min(
            broker.options_buying_power
            if broker.options_buying_power is not None
            else broker.buying_power,
            free,
        ),
        non_marginable_buying_power=min(
            broker.non_marginable_buying_power
            if broker.non_marginable_buying_power is not None
            else broker.buying_power,
            nmbp,
        ),
        options_approved_level=broker.options_approved_level,
        last_equity=last,
    )


def arm_positions(
    held: Mapping[str, Decimal], broker: Sequence[BrokerPosition]
) -> list[BrokerPosition]:
    """Shared mode: the arm's net qty per contract, marked at the broker's price.

    Never another arm's legs: the qty is the arm's own; ``current_price`` /
    ``lastday_price`` / ``change_today`` come from the broker's netted position of
    the contract, and the value fields are rescaled to the arm's qty.
    """
    by_symbol = {p.symbol: p for p in broker}
    out: list[BrokerPosition] = []
    for sym, qty in sorted(held.items()):
        if not qty:
            continue
        p = by_symbol.get(sym)
        px = p.current_price if p is not None else None
        side = "short" if qty < 0 else "long"
        option = len(sym) > 15  # an OCC symbol; else shares from an exercise/assignment
        mv = None if px is None else (qty * px * (MULTIPLIER if option else 1))
        out.append(
            BrokerPosition(
                symbol=sym,
                qty=abs(qty),
                side=side,
                market_value=mv,
                asset_class=(
                    p.asset_class if p is not None else ("us_option" if option else "us_equity")
                ),
                current_price=px,
                lastday_price=p.lastday_price if p is not None else None,
                change_today=p.change_today if p is not None else None,
            )
        )
    return out


class SharedAccountError(RuntimeError):
    """A shared-mode arm tried to act on another arm's order or contract."""


class VirtualBroker:
    """The arm's broker: the real paper broker, with ``account()`` made virtual.

    ``account()`` first appends the account's new fills and releases legacy holds
    that closed on control, then returns :func:`virtual_account`. Orders, fills,
    positions and cancels go to the real broker unchanged.
    """

    #: reconcile writes ``details_json.virtual_equity`` for a virtual broker (E10.3)
    is_virtual = True

    def __init__(
        self,
        inner: BrokerAdapter,
        conn: sqlite3.Connection,
        *,
        arm_id: str,
        t0: _dt.datetime,
        cash_settlement: bool,
        now: Callable[[], _dt.datetime],
        control: Callable[[], sqlite3.Connection | None] | None = None,
        fee_per_contract: Decimal = Decimal(0),
        account_mode: str = "dedicated",
        arm_code: str | None = None,
        account_sha256: str | None = None,
        keys_env: str = "",
    ) -> None:
        self.inner = inner
        self.conn = conn
        self.arm_id = arm_id
        self.t0 = t0
        self.cash_settlement = cash_settlement
        self._now = now
        self._control = control
        self.fee_per_contract = fee_per_contract
        self.account_mode = account_mode
        #: D69 arm tag; ``submit``/the ladder read it (``broker_arm_code``)
        self.arm_code = arm_code if account_mode == "shared" else None
        self.account_sha256 = account_sha256
        self.keys_env = keys_env
        self._book: Any = None  # last shared-mode pass (attribution.AccountBook)

    @property
    def shared(self) -> bool:
        """Shared mode with an arm code: attribution by tag (pre-E15.2 arms stay legacy)."""
        return self.arm_code is not None

    def book(self, *, refresh: bool = False) -> Any:
        """Shared mode: the account-wide attribution pass (cached per instance)."""
        from arc.experiments.attribution import account_book

        if self._book is None or refresh:
            self._book = account_book(
                self.inner,
                self._control,
                account_sha256=self.account_sha256,
                keys_env=self.keys_env,
            )
        return self._book

    def sync(self) -> VirtualState:
        now = self._now()
        if self.shared:
            book = self.book(refresh=True)
            with self.conn:
                for r in book.attribution.rows.get(self.arm_id, []):
                    _insert(self.conn, r)
        else:
            record_fills(
                self.conn,
                self.arm_id,
                self.inner.fills(self.t0),
                fee_per_contract=self.fee_per_contract,
            )
        state = replay(rows(self.conn, self.arm_id), as_of=now.astimezone(ET).date())
        if state.legacy_open and self._control is not None:
            ctl = self._control()
            if ctl is not None:
                try:
                    done = closed_legacy(ctl, state.legacy_open)
                finally:
                    ctl.close()
                if release_legacy(self.conn, self.arm_id, done, at=now):
                    state = replay(rows(self.conn, self.arm_id), as_of=now.astimezone(ET).date())
        return state

    def state(self) -> VirtualState:
        return self.sync()

    def account(self) -> AccountInfo:
        state = self.sync()
        collateral = open_collateral(self.conn)  # D129: the arm's own open structures
        info = virtual_account(
            state,
            self.inner.account(),
            self.positions(),
            cash_settlement=self.cash_settlement,
            collateral=collateral,
        )
        log.info(
            "experiments.virtual_account",
            arm_id=self.arm_id,
            equity=str(info.equity),
            spendable=str(state.spendable),
            settled=str(state.settled),
            legacy_reserved=str(state.legacy_reserved),
            collateral=str(collateral),
            options_buying_power=str(info.options_buying_power),
        )
        return info

    def positions(self) -> list[BrokerPosition]:
        """Dedicated: the broker's positions. Shared: the arm's own ledger, broker-marked."""
        broker = self.inner.positions()
        if not self.shared:
            return broker
        state = replay(rows(self.conn, self.arm_id), as_of=self._now().astimezone(ET).date())
        return arm_positions(state.held, broker)

    def fills(self, since: _dt.datetime) -> list[Fill]:
        """Shared mode: only fills of orders carrying this arm's tag."""
        out = self.inner.fills(since)
        if not self.shared:
            return out
        from arc.execution.submission import parse_order_id

        return [f for f in out if parse_order_id(f.client_order_id).arm_code == self.arm_code]

    # -- shared mode: account limits (E15.3, D69) -----------------------------------------

    def option_orders_split(self, since: _dt.datetime) -> tuple[list[Any], list[Any] | None]:
        """D32 budget on a shared account: ``(this arm's option orders, the account's)``.

        One broker call. The arm's are those whose ``client_order_id`` carries its arm
        code (E15.2's parser); dedicated mode: ``(the account's, None)`` as before.
        """
        everyone = list(self.inner.option_orders_since(since))  # type: ignore[attr-defined]
        if not self.shared:
            return everyone, None
        from arc.execution.submission import parse_order_id

        mine = [o for o in everyone if parse_order_id(o.client_order_id).arm_code == self.arm_code]
        return mine, everyone

    def cross_arm_conflict(self, order: Any) -> str | None:
        """D69: why *order* would cross another arm on the shared account, else None.

        Reads the account's open orders (``raw_orders`` since today's ET midnight) and the
        attributed holdings (:meth:`book`), then :func:`arc.experiments.conflict.find_conflict`.
        Dedicated mode: never (the arm owns the account).
        """
        if not self.shared:
            return None
        from arc.experiments.conflict import find_conflict, open_legs

        now = self._now().astimezone(ET)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        orders = self.inner.raw_orders(start)  # type: ignore[attr-defined]
        held = self.book(refresh=True).attribution.held
        return find_conflict(
            order,
            other_open=open_legs(orders, exclude_code=self.arm_code),
            other_held={a: h for a, h in held.items() if a != self.arm_id},
            own_held=held.get(self.arm_id, {}),
        )

    # -- shared mode: account-wide write calls stay inside the arm ----------------------

    def _own_order(self, client_order_id: str | None) -> None:
        from arc.execution.submission import parse_order_id

        code = parse_order_id(client_order_id).arm_code
        if code != self.arm_code:
            raise SharedAccountError(
                f"arm {self.arm_id} (code {self.arm_code}) may not touch order "
                f"{client_order_id!r} (code {code}) on the shared account"
            )

    def cancel(self, broker_order_id: str) -> None:
        """Shared mode: only an order carrying this arm's tag (fail closed otherwise)."""
        if self.shared:
            self._own_order(self.inner.order_status(broker_order_id).client_order_id)
        self.inner.cancel(broker_order_id)

    def cancel_by_client_id(self, client_order_id: str) -> None:
        if self.shared:
            self._own_order(client_order_id)
        self.inner.cancel_by_client_id(client_order_id)

    def do_not_exercise(self, occ: str) -> None:
        """Shared mode: a DNE covers the whole account's contract, so only when this
        arm is the sole holder (refused when another arm holds it too, or none)."""
        if self.shared:
            holders = sorted(
                arm
                for arm, held in self.book(refresh=True).attribution.held.items()
                if held.get(occ, Decimal(0)) != 0
            )
            if holders != [self.arm_id]:
                raise SharedAccountError(
                    f"do-not-exercise {occ}: holders on the shared account are "
                    f"{holders or 'none'}, not {self.arm_id} alone"
                )
        self.inner.do_not_exercise(occ)

    def activities(self, since: _dt.date, **kw: Any) -> list[Any]:
        """Shared mode: only the option events attributed to this arm."""
        out = self.inner.activities(since, **kw)
        if not self.shared:
            return list(out)
        mine = {
            r.ref.removeprefix("evt:").rsplit(":", 1)[0]  # evt:<activity id>:<symbol>
            for r in self.book(refresh=True).attribution.rows.get(self.arm_id, [])
            if r.kind == "event"
        }
        return [a for a in out if str(getattr(a, "id", "")) in mine]

    def book_violations(self) -> list[Any]:
        """Shared mode: the account's book invariant (``[]`` in dedicated mode)."""
        if not self.shared:
            return []
        from arc.experiments.attribution import account_violations

        return account_violations(self.book())

    def book_check(self, *, now: _dt.datetime, notifier: Any = None) -> list[Any]:
        """Reconcile's hook: check the account, halt every arm reached, return this arm's.

        Account-scope violations reach every arm (this one included); arm-scope ones
        only their arm. Fresh read of the account (``book(refresh=True)``).
        """
        if not self.shared:
            return []
        from arc.experiments.attribution import account_violations, enforce_book

        book = self.book(refresh=True)
        vs = account_violations(book)
        enforce_book(book, now=now, notifier=notifier, violations=vs)
        return [v for v in vs if v.scope == "account" or v.arm_id == self.arm_id]

    def __getattr__(self, name: str) -> Any:
        # positions, submit_mleg, cancel, order_status, fills, orders, ...
        return getattr(self.inner, name)
