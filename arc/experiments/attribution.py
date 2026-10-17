"""Shared-account attribution + the book invariant (E15.2, D69).

Under D69 every treatment arm trades one shared paper account (``ALPACA_EXP_*``).
Alpaca paper has no sub-accounts and ``/v2/positions`` is netted per contract with
no ``client_order_id``, so attribution is Arc's own:

* **Arm tag.** A shared arm's order carries ``arc2.<code>.<token rest>.s<k>``
  (:func:`arc.execution.submission.parse_order_id`); ``<code>`` is the arm's 1-char
  base62 code, allocated at t0 (:func:`free_codes`) and unique among the arms on
  the account whose attribution windows ``[t0, stop]`` overlap.
* **Fills.** ``FILL`` activities carry ``order_id``: the parent order of a simple
  order, the *leg* order of an mleg order (probed live 2026-10-12). Both map to the
  parent's ``client_order_id`` through the account's order list (nested legs), and
  the code plus the fill time pick the arm (:class:`ArmWindow`). Each activity is
  booked once, ref ``act:<activity id>``.
* **Fees.** ``FEE`` activities carry no ``order_id``. A fee whose ``execution_id``
  (legacy) or ``details.parent_id`` (Alpaca's newer schema) joins a FILL activity
  goes to that fill's arm (``method: execution_id``); otherwise the day's fees are
  split across the arms pro rata by contracts filled that ET day
  (``method: pro_rata``), to the cent by largest remainder. Live 2026-10-12: no FEE
  row on any of our three paper accounts yet (fees accrue intraday and post at EOD),
  so the join is unverified live and the pro-rata path is the default in practice.
* **Option events.** ``OPEXP`` / ``OPASN`` / ``OPEXC`` flatten the contract in the
  arms that hold it at the event, split by their held qty; the paired ``OPTRD``
  (the share leg: same day, symbol = the contract's underlying, its own id) follows
  the same split.
* **Smoke.** Orders with an Arc smoke id (``arc-<hex>``, ``test.``) belong to no
  arm; their legs sit in a ``smoke`` bucket so the invariant still sums.

:func:`check_book` ties the arms back to the broker: per contract the arms' net
qty (plus smoke) equals ``/v2/positions``; the arms' cash flows equal the broker
cash less the cash at the account's first t0 (tolerance $0.01 per fee row, widened
by pending fees below and the latest posted fee day above, see :func:`check_book`); every
order in the window carries a known arm tag or a smoke id. An untagged
leg/order/fill and any cash drift halt **every** arm on the account; a qty drift on
a contract only one arm holds halts that arm only (:func:`halt_violations`).

Everything here is pure except :func:`account_windows` (reads the arm stores
read-only) and :func:`halt_violations` (writes halts + journal rows to arm stores).
"""

from __future__ import annotations

import datetime as _dt
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, cast

import structlog
from pydantic import BaseModel, ConfigDict, Field

from arc.broker.base import TEST_CLIENT_ORDER_PREFIX, BrokerPosition
from arc.context.ttl import from_db, to_db
from arc.execution.submission import ARM_CODES, parse_order_id
from arc.experiments.virtual import MULTIPLIER, LedgerRow
from arc.utils.calendar import ET, next_session

__all__ = [
    "BOOK_INVARIANT",
    "SMOKE",
    "Activity",
    "ArmWindow",
    "Attribution",
    "BookViolation",
    "CodeAllocationError",
    "account_windows",
    "attribute",
    "check_book",
    "free_codes",
    "halt_violations",
    "order_tags",
    "parse_activity",
    "split_cents",
]

log = structlog.get_logger(__name__)

#: halt reason prefix + journal reason of a failed book invariant
BOOK_INVARIANT = "book_invariant"
#: the bucket of Arc smoke-test orders on the shared account (no arm)
SMOKE = "smoke"
_SMOKE_PREFIXES = ("arc-", TEST_CLIENT_ORDER_PREFIX)
_CENT = Decimal("0.01")
_NTA_OPTION = ("OPEXP", "OPASN", "OPEXC")
ActivityType = Literal["FILL", "FEE", "OPEXP", "OPASN", "OPEXC", "OPTRD"]
ACTIVITY_TYPES: tuple[str, ...] = ("FILL", "FEE", "OPEXP", "OPASN", "OPEXC", "OPTRD")
#: date-only activities (FEE, NTAs) are placed after the session's fills
_DATE_ONLY_TIME = _dt.time(16, 0)
#: upper bound on a contract's accrued, not-yet-posted regulatory fees (OCC + ORF + TAF
#: + CAT); observed $0.0254/contract on the production paper account 2026-10-12
PENDING_FEE_PER_CONTRACT = Decimal("0.05")


class CodeAllocationError(RuntimeError):
    """No free arm code on the account (62 codes; the D69 hard ceiling is 16 arms)."""


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


class Activity(BaseModel):
    """One Alpaca account activity the attributor reads (``FILL`` / ``FEE`` / NTAs)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    activity_type: ActivityType
    at: _dt.datetime = Field(..., description="transaction_time, or the date at 16:00 ET")
    day: _dt.date = Field(..., description="ET day")
    symbol: str = ""
    side: str | None = None
    qty: Decimal = Decimal(0)
    price: Decimal | None = None
    order_id: str | None = None
    net_amount: Decimal | None = None
    execution_id: str | None = None


def _dec(v: object) -> Decimal | None:
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def parse_activity(raw: object) -> Activity | None:
    """One raw Alpaca activity row -> :class:`Activity` (``None`` = not one we attribute)."""
    if not isinstance(raw, Mapping):
        return None
    kind = str(raw.get("activity_type") or "")
    if kind not in ACTIVITY_TYPES:
        return None
    ts = raw.get("transaction_time")
    try:
        if ts:
            at = _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        else:
            d = _dt.date.fromisoformat(str(raw.get("date") or "")[:10])
            at = _dt.datetime.combine(d, _DATE_ONLY_TIME, tzinfo=ET)
    except ValueError:
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=_dt.UTC)
    day = (
        _dt.date.fromisoformat(str(raw["date"])[:10])
        if raw.get("date")
        else at.astimezone(ET).date()
    )
    details = raw.get("details") if isinstance(raw.get("details"), Mapping) else {}
    link = raw.get("execution_id") or details.get("parent_id")  # type: ignore[union-attr]
    return Activity(
        id=str(raw.get("id") or f"{kind}:{raw.get('symbol', '')}:{at.isoformat()}"),
        activity_type=kind,  # type: ignore[arg-type]
        at=at,
        day=day,
        symbol=str(raw.get("symbol") or ""),
        side=str(raw["side"]).lower() if raw.get("side") else None,
        qty=_dec(raw.get("qty")) or Decimal(0),
        price=_dec(raw.get("price")),
        order_id=str(raw["order_id"]) if raw.get("order_id") else None,
        net_amount=_dec(raw.get("net_amount")),
        execution_id=str(link) if link else None,
    )


def order_tags(orders: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """``order id -> client_order_id`` for every parent order AND each nested leg.

    An mleg leg's FILL activity carries the leg's own ``id``; the leg inherits the
    parent's ``client_order_id`` (the leg's own one is Alpaca-generated).
    """
    out: dict[str, str] = {}
    for o in orders:
        coid = str(o.get("client_order_id") or "")
        if o.get("id"):
            out[str(o["id"])] = coid
        for leg in o.get("legs") or []:
            if isinstance(leg, Mapping) and leg.get("id"):
                out[str(leg["id"])] = coid
    return out


class ArmWindow(BaseModel):
    """One arm on the shared account: its code and attribution window ``[t0, stop]``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    arm_id: str
    code: str
    t0: _dt.datetime
    stop: _dt.datetime | None = None
    running: bool = True
    cash_t0: Decimal | None = None
    store: str | None = None

    def covers(self, at: _dt.datetime) -> bool:
        return self.t0 <= at and (self.stop is None or at <= self.stop)


def _root(occ: str) -> str:
    """The underlying of an OCC symbol (``QS240301C00006500`` -> ``QS``)."""
    return occ[:-15] if len(occ) > 15 else occ


def _is_smoke(coid: str) -> bool:
    return coid.startswith(_SMOKE_PREFIXES)


# ---------------------------------------------------------------------------
# Code allocation (t0)
# ---------------------------------------------------------------------------


def free_codes(taken: Iterable[str], n: int) -> list[str]:
    """The first *n* base62 codes not in *taken* (allocation order = :data:`ARM_CODES`).

    Raises :class:`CodeAllocationError` (fail closed) when fewer than *n* are free.
    """
    used = set(taken)
    free = [c for c in ARM_CODES if c not in used]
    if len(free) < n:
        msg = (
            f"no free arm code: {len(used)} of {len(ARM_CODES)} codes are held by arms "
            f"that are running or not flat on this account; {n} needed"
        )
        raise CodeAllocationError(msg)
    return free[:n]


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


def split_cents(total: Decimal, weights: Mapping[str, Decimal]) -> dict[str, Decimal]:
    """Split *total* dollars by *weights* to the cent; the parts sum to *total* exactly.

    Largest remainder, ties by key: deterministic, so a re-run books the same rows.
    """
    keys = sorted(k for k, w in weights.items() if w > 0)
    if not keys:
        return {}
    wsum = sum((weights[k] for k in keys), Decimal(0))
    cents = int((total / _CENT).to_integral_value())
    sign = -1 if cents < 0 else 1
    cents = abs(cents)
    raw = {k: Decimal(cents) * weights[k] / wsum for k in keys}
    base = {k: int(raw[k].to_integral_value(rounding=ROUND_FLOOR)) for k in keys}
    left = cents - sum(base.values())
    for k in sorted(keys, key=lambda k: (-(raw[k] - base[k]), k))[:left]:
        base[k] += 1
    return {k: sign * Decimal(v) * _CENT for k, v in base.items()}


def _split_qty(total: Decimal, holdings: Mapping[str, Decimal]) -> dict[str, Decimal]:
    """Split *total* contracts by holdings (whole contracts, largest remainder)."""
    keys = sorted(k for k, h in holdings.items() if h)
    if not keys:
        return {}
    hsum = sum((abs(holdings[k]) for k in keys), Decimal(0))
    if total >= hsum:
        return {k: abs(holdings[k]) for k in keys}
    raw = {k: total * abs(holdings[k]) / hsum for k in keys}
    base = {k: raw[k].to_integral_value(rounding=ROUND_FLOOR) for k in keys}
    left = int(total - sum(base.values()))
    for k in sorted(keys, key=lambda k: (-(raw[k] - base[k]), k))[:left]:
        base[k] += 1
    return base


class Attribution(BaseModel):
    """What one pass over the account's activities attributed (see the module doc)."""

    model_config = ConfigDict(extra="forbid")

    rows: dict[str, list[LedgerRow]] = Field(default_factory=dict, description="arm id -> rows")
    held: dict[str, dict[str, Decimal]] = Field(
        default_factory=dict, description="bucket (arm id | smoke) -> symbol -> net qty"
    )
    cash: dict[str, Decimal] = Field(default_factory=dict, description="bucket -> cash flow")
    fees: dict[str, Decimal] = Field(default_factory=dict, description="bucket -> fees")
    fee_rows: int = 0
    unfeed_contracts: Decimal = Field(
        Decimal(0), description="contracts filled on ET days with no FEE activity posted yet"
    )
    latest_fee_day_total: Decimal = Field(
        Decimal(0),
        description=(
            "absolute sum of FEE net_amount on the most recent ET day with FEE rows (0 when "
            "none): fees Alpaca may have posted before /v2/account cash reflects them"
        ),
    )
    untagged: list[str] = Field(default_factory=list, description="activities no arm owns")
    by_type: dict[str, int] = Field(default_factory=dict)

    def arm_held(self, arm_id: str) -> dict[str, Decimal]:
        return {k: v for k, v in self.held.get(arm_id, {}).items() if v}


def _fill_amount(side: str, qty: Decimal, price: Decimal) -> Decimal:
    gross = (price * qty * MULTIPLIER).quantize(_CENT)
    return gross if side.startswith("sell") else -gross


def attribute(
    activities: Iterable[Activity],
    tags: Mapping[str, str],
    windows: Sequence[ArmWindow],
) -> Attribution:
    """Attribute every activity on the account to an arm, the smoke bucket, or nobody.

    Pure: the same inputs book the same rows (refs are activity ids), so each arm
    store may run it and append only its own rows (``INSERT OR IGNORE``).
    """
    out = Attribution()
    held: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    cash: dict[str, Decimal] = defaultdict(Decimal)
    fees: dict[str, Decimal] = defaultdict(Decimal)
    rows: dict[str, list[LedgerRow]] = defaultdict(list)
    by_type: dict[str, int] = defaultdict(int)
    filled_by_day: dict[_dt.date, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    fill_bucket: dict[str, str] = {}  # activity id (and its uuid tail) -> bucket
    event_split: dict[str, dict[str, Decimal]] = {}  # NTA id -> bucket -> contracts
    # exercise/assignment events still waiting for their OPTRD share leg
    pending_trades: list[tuple[_dt.date, str, dict[str, Decimal]]] = []
    # Pass order: every FILL (holdings + the day's contract counts), then option events
    # (a contract's holdings at its expiry are its final fills), their OPTRD share
    # legs, then FEEs (pro rata over the whole ET day; Alpaca posts them at EOD).
    rank = {"FILL": 0, "OPEXP": 1, "OPASN": 1, "OPEXC": 1, "OPTRD": 2, "FEE": 3}
    acts = sorted(
        {a.id + a.activity_type + a.symbol: a for a in activities}.values(),
        key=lambda a: (rank[a.activity_type], a.at, a.id),
    )

    def bucket_of(order_id: str | None, at: _dt.datetime) -> str | None:
        coid = tags.get(order_id or "")
        if coid is None:
            return None
        if _is_smoke(coid):
            return SMOKE
        code = parse_order_id(coid).arm_code
        if code is None:
            return None
        # codes are reused only once the earlier arm is stopped AND flat, so the
        # latest arm with this code whose t0 precedes the fill owns it
        hits = [w for w in windows if w.code == code and w.t0 <= at]
        return max(hits, key=lambda w: w.t0).arm_id if hits else None

    def book(bucket: str, row: LedgerRow) -> None:
        if bucket != SMOKE:
            rows[bucket].append(row)

    for a in acts:
        by_type[a.activity_type] += 1
        if a.activity_type == "FILL":
            b = bucket_of(a.order_id, a.at)
            if b is None or a.price is None or not a.side:
                out.untagged.append(
                    f"FILL {a.id} {a.symbol} {a.side} {a.qty} order {a.order_id} "
                    f"({tags.get(a.order_id or '', 'unknown order')!s:.40})"
                )
                continue
            sign = Decimal(-1) if a.side.startswith("sell") else Decimal(1)
            amount = _fill_amount(a.side, abs(a.qty), a.price)
            held[b][a.symbol] += sign * abs(a.qty)
            cash[b] += amount
            filled_by_day[a.day][b] += abs(a.qty)
            fill_bucket[a.id] = b
            fill_bucket[a.id.rsplit("::", 1)[-1]] = b
            book(
                b,
                LedgerRow(
                    arm_id=b,
                    kind="fill",
                    ref=f"act:{a.id}",
                    amount=amount,
                    settles_on=next_session(a.day) if amount > 0 else a.day,
                    at=a.at,
                    detail={
                        "symbol": a.symbol,
                        "qty": str(sign * abs(a.qty)),
                        "price": str(a.price),
                        "order_id": a.order_id,
                        "client_order_id": tags.get(a.order_id or ""),
                        "source": "FILL",
                    },
                ),
            )
        elif a.activity_type == "FEE":
            out.fee_rows += 1
            amt = a.net_amount if a.net_amount is not None else Decimal(0)
            joined = fill_bucket.get(a.execution_id or "")
            if joined is not None:
                parts, method = {joined: amt}, "execution_id"
            else:
                weights = dict(filled_by_day.get(a.day, {}))
                if not weights:  # a fee on a day with no fill: every live arm, equally
                    weights = {w.arm_id: Decimal(1) for w in windows if w.covers(a.at)}
                parts, method = split_cents(amt, weights), "pro_rata"
            if not parts:
                out.untagged.append(f"FEE {a.id} {amt} on {a.day}: no arm to charge")
                continue
            for b, part in parts.items():
                cash[b] += part
                fees[b] += part
                book(
                    b,
                    LedgerRow(
                        arm_id=b,
                        kind="fee",
                        ref=f"fee:{a.id}",
                        amount=part,
                        settles_on=a.day,
                        at=a.at,
                        detail={
                            "method": method,
                            "fee_total": str(amt),
                            "execution_id": a.execution_id,
                            "symbol": "",
                            "qty": "0",
                        },
                    ),
                )
        elif a.activity_type in _NTA_OPTION:
            holders = {b: h[a.symbol] for b, h in held.items() if h.get(a.symbol)}
            split = _split_qty(abs(a.qty), holders)
            if not split:
                out.untagged.append(f"{a.activity_type} {a.id} {a.symbol} {a.qty}: no arm holds it")
                continue
            event_split[a.id] = split
            if a.activity_type != "OPEXP":  # exercise / assignment: a share leg follows
                pending_trades.append((a.day, _root(a.symbol), split))
            for b, n in split.items():
                delta = -n if holders[b] > 0 else n
                held[b][a.symbol] += delta
                book(
                    b,
                    LedgerRow(
                        arm_id=b,
                        kind="event",
                        ref=f"evt:{a.id}:{a.symbol}",
                        amount=Decimal(0),
                        settles_on=a.day,
                        at=a.at,
                        detail={
                            "activity_type": a.activity_type,
                            "symbol": a.symbol,
                            "qty": str(delta),
                            "event_qty": str(a.qty),
                        },
                    ),
                )
        else:  # OPTRD: the share leg of an exercise/assignment (same day + underlying)
            match = next(
                (i for i, (d, root, _) in enumerate(pending_trades)
                 if d == a.day and root == a.symbol),
                None,
            )  # fmt: skip
            if match is None:
                out.untagged.append(f"OPTRD {a.id} {a.symbol} {a.qty}: no option event")
                continue
            split = pending_trades.pop(match)[2]
            amt = a.net_amount if a.net_amount is not None else Decimal(0)
            money = split_cents(amt, split)
            total = sum(split.values(), Decimal(0))
            for b, n in sorted(split.items()):
                shares = a.qty * n / total
                held[b][a.symbol] += shares
                cash[b] += money.get(b, Decimal(0))
                book(
                    b,
                    LedgerRow(
                        arm_id=b,
                        kind="event",
                        ref=f"evt:{a.id}:{a.symbol}",
                        amount=money.get(b, Decimal(0)),
                        settles_on=next_session(a.day) if amt > 0 else a.day,
                        at=a.at,
                        detail={
                            "activity_type": "OPTRD",
                            "symbol": a.symbol,
                            "qty": str(shares),
                            "price": str(a.price) if a.price is not None else None,
                        },
                    ),
                )
    out.rows = {k: v for k, v in rows.items()}
    fee_days = {a.day for a in acts if a.activity_type == "FEE"}
    if fee_days:
        last = max(fee_days)
        out.latest_fee_day_total = abs(
            sum(
                (a.net_amount or Decimal(0) for a in acts
                 if a.activity_type == "FEE" and a.day == last),
                Decimal(0),
            )
        )  # fmt: skip
    out.unfeed_contracts = sum(
        (sum(per.values(), Decimal(0)) for d, per in filled_by_day.items() if d not in fee_days),
        Decimal(0),
    )
    out.held = {b: {s: q for s, q in h.items() if q} for b, h in held.items()}
    out.cash = dict(cash)
    out.fees = dict(fees)
    out.by_type = dict(by_type)
    return out


# ---------------------------------------------------------------------------
# The book invariant
# ---------------------------------------------------------------------------


class BookViolation(BaseModel):
    """One failed book-invariant check. ``scope='account'`` halts every arm on it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: Literal["account", "arm"]
    arm_id: str | None = None
    kind: Literal["untagged", "order", "qty", "cash"]
    subject: str
    detail: str


def _signed(p: BrokerPosition) -> Decimal:
    return -abs(p.qty) if p.side == "short" else abs(p.qty)


def check_book(
    attr: Attribution,
    positions: Sequence[BrokerPosition],
    *,
    broker_cash: Decimal | None,
    cash_baseline: Decimal | None,
    orders: Iterable[Mapping[str, Any]] = (),
    windows: Sequence[ArmWindow] = (),
    since: _dt.datetime | None = None,
    pending_fee_per_contract: Decimal = PENDING_FEE_PER_CONTRACT,
) -> list[BookViolation]:
    """The D69 book invariant over one shared account (see the module doc).

    Cash: Alpaca accrues fees intraday and posts the ``FEE`` activity at EOD, so the
    broker's cash may run below the arms' flows by the fees of contracts filled on
    days with no FEE row yet (probed 2026-10-12: 13 contracts, $0.33 pending). The
    opposite lag happens too: Alpaca can post a day's ``FEE`` rows (booked to the arms)
    before ``/v2/account`` cash includes them (2026-10-15 03:10 ET: +$0.57 against
    $1.27 of 10-14 fees, 0.00 by 07:37), so the upper bound adds the latest fee day's
    total. The residual must lie in ``[-(pending + tol), +(tol + latest_fee_day)]``;
    ``tol`` = $0.01 x fee rows.
    """
    out: list[BookViolation] = [
        BookViolation(scope="account", kind="untagged", subject="activity", detail=u)
        for u in attr.untagged
    ]
    broker = defaultdict(Decimal)
    for p in positions:
        broker[p.symbol] += _signed(p)
    symbols = set(broker) | {s for h in attr.held.values() for s in h}
    for sym in sorted(symbols):
        mine = {b: h.get(sym, Decimal(0)) for b, h in attr.held.items() if h.get(sym)}
        total = sum(mine.values(), Decimal(0))
        if total == broker.get(sym, Decimal(0)):
            continue
        arms = [b for b in mine if b != SMOKE]
        detail = (
            f"{sym}: arms {' + '.join(f'{b} {q}' for b, q in sorted(mine.items())) or 'none'}"
            f" = {total}, broker {broker.get(sym, Decimal(0))}"
        )
        if len(mine) == 1 and arms:
            out.append(BookViolation(scope="arm", arm_id=arms[0], kind="qty", subject=sym,
                                     detail=detail))  # fmt: skip
        else:
            out.append(BookViolation(scope="account", kind="qty", subject=sym, detail=detail))
    if broker_cash is not None and cash_baseline is not None:
        flows = sum(attr.cash.values(), Decimal(0))
        want = broker_cash - cash_baseline
        tol = _CENT * max(attr.fee_rows, 1)
        pending = (pending_fee_per_contract * attr.unfeed_contracts).quantize(_CENT)
        posted = attr.latest_fee_day_total
        residual = want - flows  # negative = the broker charged more than the arms booked
        if not (-(pending + tol) <= residual <= tol + posted):
            out.append(
                BookViolation(
                    scope="account",
                    kind="cash",
                    subject="cash",
                    detail=(
                        f"arms' cash flows {flows} vs broker cash {broker_cash} - t0 cash "
                        f"{cash_baseline} = {want} (residual {residual}, allowed "
                        f"[-{pending + tol}, +{tol + posted}]: pending {pending} + tol "
                        f"{tol} below, tol {tol} + latest fee day {posted} above)"
                    ),
                )
            )
    for o in orders:
        at_raw = o.get("submitted_at") or o.get("created_at")
        try:
            at = _dt.datetime.fromisoformat(str(at_raw).replace("Z", "+00:00"))
        except ValueError:
            at = None
        if since is not None and at is not None and at < since:
            continue
        coid = str(o.get("client_order_id") or "")
        if _is_smoke(coid):
            continue
        code = parse_order_id(coid).arm_code
        known = code is not None and (
            at is None or any(w.code == code and w.t0 <= at for w in windows)
        )
        if not known:
            out.append(
                BookViolation(
                    scope="account",
                    kind="order",
                    subject=str(o.get("id") or ""),
                    detail=f"order {o.get('id')} client id {coid[:48]!r} carries no live arm tag",
                )
            )
    return out


# ---------------------------------------------------------------------------
# Store I/O: the arms on one account, and halting them
# ---------------------------------------------------------------------------


def account_windows(
    control: sqlite3.Connection,
    *,
    account_sha256: str | None,
    keys_env: str,
) -> list[ArmWindow]:
    """Every shared-mode arm ever started on the account (running or stopped), oldest first.

    Read-only. The account is matched on ``account_sha256`` when both sides have one,
    else on ``keys_env``. A stopped arm's window ends at its experiment's stop event.
    """
    from arc.experiments.arms import arm_stores, read_identity
    from arc.experiments.models import ExperimentStatus
    from arc.experiments.store import ExperimentStore

    if control.row_factory is None:  # ExperimentStore reads rows by name
        control.row_factory = sqlite3.Row
    store = ExperimentStore(control)
    out: list[ArmWindow] = []
    for path in arm_stores(control).values():
        conn = _ro(path)
        if conn is None:
            continue
        try:
            ident = read_identity(conn)
            r = conn.execute(
                "SELECT at FROM virtual_ledger WHERE kind = 'open' ORDER BY id LIMIT 1"
            ).fetchone()
        except sqlite3.Error:
            continue
        finally:
            conn.close()
        if ident is None or ident.account_mode != "shared" or not ident.arm_code or r is None:
            continue
        same = (
            ident.account_sha256 == account_sha256
            if ident.account_sha256 is not None and account_sha256 is not None
            else ident.keys_env == keys_env
        )
        if not same:
            continue
        st = store.get(ident.experiment_id)
        running = st is not None and st.status is ExperimentStatus.RUNNING
        stop = None
        if st is not None and not running:
            started = [i for i, e in enumerate(st.events) if e.status is ExperimentStatus.RUNNING]
            after = st.events[started[-1] + 1 :] if started else []
            stop = after[0].at if after else None
        out.append(
            ArmWindow(
                arm_id=ident.arm_id,
                code=ident.arm_code,
                t0=from_db(r[0]),
                stop=stop,
                running=running,
                cash_t0=ident.account_cash_t0,
                store=str(path),
            )
        )
    return sorted(out, key=lambda w: (w.t0, w.arm_id))


def held_codes(windows: Sequence[ArmWindow]) -> set[str]:
    """Codes not reusable: the arm is running, or stopped but not flat in its ledger."""
    from arc.experiments.virtual import replay, rows

    taken: set[str] = set()
    for w in windows:
        if w.running:
            taken.add(w.code)
            continue
        conn = _ro(Path(w.store)) if w.store else None
        if conn is None:  # store gone: cannot prove flat, keep the code
            taken.add(w.code)
            continue
        try:
            state = replay(rows(conn, w.arm_id), as_of=_dt.date.max)
        except (sqlite3.Error, ValueError):
            taken.add(w.code)
            continue
        finally:
            conn.close()
        if state.held:
            taken.add(w.code)
    return taken


def cash_baseline(windows: Sequence[ArmWindow]) -> Decimal | None:
    """The broker cash at the shared account's first t0 (None when not recorded)."""
    return windows[0].cash_t0 if windows else None


class AccountBook(BaseModel):
    """One read of a shared account + its attribution (read-only broker calls)."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    windows: list[ArmWindow]
    attribution: Attribution
    positions: list[BrokerPosition]
    broker_cash: Decimal | None
    cash_baseline: Decimal | None
    orders: list[dict[str, Any]] = Field(default_factory=list)
    since: _dt.datetime | None = None

    def totals(self) -> dict[str, dict[str, str]]:
        """Per bucket: cash flow, fees, contracts held (the live-replay report)."""
        out: dict[str, dict[str, str]] = {}
        for b in sorted(set(self.attribution.cash) | set(self.attribution.held)):
            held = self.attribution.held.get(b, {})
            out[b] = {
                "cash_flow": str(self.attribution.cash.get(b, Decimal(0))),
                "fees": str(self.attribution.fees.get(b, Decimal(0))),
                "contracts": str(sum((abs(q) for q in held.values()), Decimal(0))),
            }
        return out


def build_book(
    raw_activities: Iterable[object],
    raw_orders: Sequence[Mapping[str, Any]],
    windows: Sequence[ArmWindow],
    positions: Sequence[BrokerPosition],
    *,
    broker_cash: Decimal | None,
    since: _dt.datetime | None = None,
) -> AccountBook:
    """Pure: raw Alpaca rows + the account's arm windows -> :class:`AccountBook`."""
    acts = [a for a in (parse_activity(r) for r in raw_activities) if a is not None]
    if since is not None:
        acts = [a for a in acts if a.at >= since or a.day >= since.astimezone(ET).date()]
    attr = attribute(acts, order_tags(raw_orders), windows)
    return AccountBook(
        windows=list(windows),
        attribution=attr,
        positions=list(positions),
        broker_cash=broker_cash,
        cash_baseline=cash_baseline(windows),
        orders=[dict(o) for o in raw_orders],
        since=since,
    )


def account_violations(book: AccountBook) -> list[BookViolation]:
    """The book invariant over *book* (see :func:`check_book`)."""
    return check_book(
        book.attribution,
        book.positions,
        broker_cash=book.broker_cash,
        cash_baseline=book.cash_baseline,
        orders=book.orders,
        windows=book.windows,
        since=book.since,
    )


def account_book(
    broker: Any,
    control: Any,
    *,
    account_sha256: str | None,
    keys_env: str,
) -> AccountBook:
    """Read the shared account (activities, orders, positions, cash) and attribute it.

    *control* is a zero-arg opener of the control store (read-only) or a connection.
    Read-only on the broker: ``raw_activities`` / ``raw_orders`` / ``positions`` /
    ``account``. Fails loudly when the broker cannot list raw activities.
    """
    opener = not isinstance(control, sqlite3.Connection) and callable(control)
    conn = cast("sqlite3.Connection | None", control() if opener else control)
    try:
        windows = (
            account_windows(conn, account_sha256=account_sha256, keys_env=keys_env)
            if conn is not None
            else []
        )
    finally:
        if opener and conn is not None:
            conn.close()
    since = windows[0].t0 if windows else None
    if since is None:  # no shared arm on the account: nothing to attribute
        return build_book([], [], [], broker.positions(), broker_cash=None)
    raw = broker.raw_activities(since.astimezone(ET).date())
    orders = broker.raw_orders(since)
    return build_book(
        raw,
        orders,
        windows,
        broker.positions(),
        broker_cash=broker.account().cash,
        since=since,
    )


def enforce_book(
    book: AccountBook,
    *,
    now: _dt.datetime,
    notifier: Any = None,
    violations: Sequence[BookViolation] | None = None,
) -> dict[str, list[BookViolation]]:
    """Check the invariant over *book* and halt the reached running arms (their stores).

    Opens each running arm's store read-write by its recorded path; control is never
    on the list. Returns ``arm id -> violations`` for the arms halted now.
    """
    from arc.store.db import connect

    vs = list(violations) if violations is not None else account_violations(book)
    if not vs:
        return {}
    conns: dict[str, sqlite3.Connection] = {}
    try:
        for w in book.windows:
            if w.running and w.store and Path(w.store).is_file():
                conns[w.arm_id] = connect(Path(w.store))
        return halt_violations(vs, conns, now=now, notifier=notifier)
    finally:
        for c in conns.values():
            c.close()


def _ro(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    try:
        return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def halt_violations(
    violations: Sequence[BookViolation],
    arms: Mapping[str, sqlite3.Connection],
    *,
    now: _dt.datetime,
    notifier: Any = None,
    actor: str = "arc:experiments",
) -> dict[str, list[BookViolation]]:
    """Halt the arms *violations* reach: account scope -> every arm in *arms*.

    Never touches control (it is not on the shared account). One active
    ``book_invariant`` halt per arm store; each new halt gets a journal row and one
    ``[Ops]`` notice. Returns ``arm id -> violations`` for the arms halted now.
    """
    from arc.journal.reasons import Choice, JournalPersona, ReasonCode, Stage
    from arc.journal.store import JournalStore
    from arc.store.repos import HaltRepo

    per_arm: dict[str, list[BookViolation]] = defaultdict(list)
    for v in violations:
        targets = list(arms) if v.scope == "account" else [v.arm_id or ""]
        for a in targets:
            if a in arms:
                per_arm[a].append(v)
    halted: dict[str, list[BookViolation]] = {}
    for arm_id, vs in sorted(per_arm.items()):
        conn = arms[arm_id]
        repo = HaltRepo(conn)
        if any(str(h.get("reason", "")).startswith(BOOK_INVARIANT) for h in repo.active()):
            continue
        kinds = sorted({f"{v.scope}:{v.kind}" for v in vs})
        reason = f"{BOOK_INVARIANT}: {len(vs)} violation(s) ({', '.join(kinds)})"
        repo.halt(reason=reason, actor=actor, kind="manual", at=to_db(now))
        store = JournalStore(conn)
        with conn:
            for v in vs:
                store.record(
                    persona=JournalPersona.BROKER,
                    stage=Stage.RECONCILE,
                    subject=v.subject,
                    choice=Choice.FAILED,
                    reason_code=ReasonCode.RECONCILE_BOOK_INVARIANT,
                    reason_text=v.detail[:2000],
                    at=now,
                    payload={"scope": v.scope, "kind": v.kind, "arm_id": v.arm_id},
                )
        halted[arm_id] = vs
        if notifier is not None:
            notifier.post(
                f"[Ops] Experiment arm {arm_id} halted ({BOOK_INVARIANT}): "
                + "; ".join(v.detail for v in vs[:3])
                + (f" (+{len(vs) - 3} more)" if len(vs) > 3 else "")
            )
        log.error("experiments.book_invariant_halt", arm_id=arm_id, kinds=kinds)
    return halted
