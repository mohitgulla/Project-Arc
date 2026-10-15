"""E10.2e: an arm store sees control's reference tables (betas, iv_daily, earnings docs).

Live 2026-10-13 (XP-11 A/A): control's gate failed an MU put debit spread on the β$Δ
cap (MU β 3.20) while every arm passed it, because the arm stores held no ``betas``
row (β fell back to 1.0), no ``iv_daily`` and no earnings calendar docs.
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
from decimal import Decimal as D
from pathlib import Path

import pytest

from arc.betas.store import BetaRow, betas_used
from arc.betas.store import upsert as upsert_betas
from arc.experiments.reference import (
    ARM_OWN_TABLES,
    ARM_REFERENCE_TABLES,
    sync_reference_tables,
)
from arc.gate import Portfolio
from arc.gate import rules as R
from arc.gate.rules import RuleCode
from arc.ingest.store import FILTERED_STATUS, RawDocRepo
from arc.iv.store import BACKFILL, FORWARD, IvRow, IvStore, safe_store
from arc.pipeline.market import next_earnings, proposal_betas
from arc.scanner.iv import iv_rank
from arc.store.migrate import migrate
from arc.utils.calendar import ET
from tests.test_gate import _call, acct, cfg, mkt

DAY = dt.date(2026, 10, 13)
CHAIN = dt.datetime(2026, 10, 13, 12, 30, tzinfo=ET)  # 16:30Z
BEFORE = dt.datetime(2026, 10, 13, 7, 10, tzinfo=ET)
AFTER = dt.datetime(2026, 10, 13, 13, 0, tzinfo=ET)
ROOT = Path(__file__).resolve().parents[1]


def _db(path: Path | str = ":memory:") -> sqlite3.Connection:
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    migrate(c)
    return c


def _beta(ticker: str, day: dt.date, beta: float) -> BetaRow:
    return BetaRow(ticker, day, beta, 252, 252, "SPY", day - dt.timedelta(days=1))


def _iv(ticker: str, day: dt.date, iv: float, source: str = BACKFILL) -> IvRow:
    return IvRow(ticker=ticker, day=day, iv30=iv, method="bars_bs_cm30", source=source)  # type: ignore[arg-type]


def _earnings(store: RawDocRepo, sym: str, day: dt.date) -> str | None:
    return store.insert(
        source="earnings",
        url=f"https://finnhub.io/calendar/earnings/{sym}/{day.isoformat()}",
        published_at=f"{day.isoformat()}T00:00:00+00:00",
        text=f"{sym} earnings {day}",
        tickers_hint=[sym],
        closed_status=FILTERED_STATUS,
    )


def _set_ingested(conn: sqlite3.Connection, doc_id: str, at: dt.datetime) -> None:
    with conn:
        conn.execute(
            "UPDATE raw_docs SET ingested_at = ? WHERE id = ?",
            (at.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"), doc_id),
        )


@pytest.fixture
def control() -> sqlite3.Connection:
    c = _db()
    upsert_betas(c, [_beta("MU", DAY, 3.2), _beta("KO", DAY, 0.55)], now=BEFORE)
    upsert_betas(c, [_beta("MU", DAY - dt.timedelta(days=1), 3.1)], now=BEFORE)
    upsert_betas(c, [_beta("NVDA", DAY + dt.timedelta(days=1), 2.0)], now=AFTER)  # future
    ivs = IvStore(c)
    hist = [_iv("MU", DAY - dt.timedelta(days=i), 0.40 + 0.01 * (i % 17)) for i in range(1, 200)]
    ivs.upsert([*hist, _iv("MU", DAY, 0.55, FORWARD)], now=BEFORE)
    ivs.upsert([_iv("MU", DAY + dt.timedelta(days=1), 0.9, FORWARD)], now=AFTER)
    ivs.skip("MU", DAY - dt.timedelta(days=300), BACKFILL, "no bracket", now=BEFORE)
    docs = RawDocRepo(c)
    for sym, day in (("MU", dt.date(2026, 12, 17)), ("MU", dt.date(2026, 9, 30)),
                     ("UNH", dt.date(2026, 10, 15))):  # fmt: skip
        _set_ingested(c, _earnings(docs, sym, day) or "", BEFORE)
    late = _earnings(docs, "JNJ", dt.date(2026, 10, 14)) or ""
    _set_ingested(c, late, AFTER)  # pulled after the chain: not known yet
    docs.insert(source="rss", url="https://x/1", published_at="2026-10-13T10:00:00Z", text="n")
    c.commit()
    return c


def _ro(conn: sqlite3.Connection, tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "control.db"
    conn.execute("VACUUM INTO ?", (str(path),))
    ro = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    return ro


def test_beta_cap_parity_mu_fails_in_the_arm_as_in_control(
    control: sqlite3.Connection, tmp_path: Path
) -> None:
    """The live MU case in miniature: control's β 3.20 fails the cap; the unsynced arm
    sees β 1.0 and passes; after the sync the arm fails it with control's detail."""
    arm = _db()
    book = Portfolio(dollar_delta=D(-40_000), beta_dollar_delta=D(-60_000))
    p = _call("MU", -300.0)  # -$30k of MU at spot 100
    c = cfg(portfolio_beta_delta_cap_pct=1.0, portfolio_dollar_delta_cap_pct=2.0)

    def gate(conn: sqlite3.Connection) -> list[tuple[str, str]]:
        m = mkt(underlying_spot={"MU": D(100)}, underlying_beta=proposal_betas(conn, ["MU"], DAY))
        return [(v.code, v.detail) for v in R.check_greek_caps(p, acct(), book, m, c)]

    want = [(RuleCode.BETA_DELTA_CAP, "post-trade |β$Δ| $156,000.00 > cap $100,000.00 (MU β 3.20)")]
    assert gate(control) == want
    assert gate(arm) == []  # the live bug: β 1.0 -> |β$Δ| $90k passes
    assert betas_used(arm, ["MU"], DAY)["MU"].source == "default"

    synced = sync_reference_tables(_ro(control, tmp_path), arm, as_of=CHAIN)
    assert synced["betas"] == 3  # MU today + yesterday, KO; never NVDA's future row
    assert gate(arm) == want
    assert betas_used(arm, ["MU", "KO"], DAY) == betas_used(control, ["MU", "KO"], DAY)
    assert arm.execute("SELECT count(*) FROM betas WHERE ticker = 'NVDA'").fetchone()[0] == 0


def test_earnings_parity(control: sqlite3.Connection) -> None:
    arm = _db()
    tickers = ["MU", "UNH", "JNJ", "SPY"]
    assert next_earnings(arm, tickers, DAY) == {"SPY": None}  # unknown: fails closed
    sync_reference_tables(control, arm, as_of=CHAIN)
    got = next_earnings(arm, tickers, DAY)
    assert got == {"MU": dt.date(2026, 12, 17), "UNH": dt.date(2026, 10, 15), "SPY": None}
    # JNJ's date was pulled after the chain: unknown in the arm as of the chain
    assert sync_reference_tables(control, arm, as_of=AFTER)["raw_docs"] == 1
    assert next_earnings(arm, tickers, DAY) == next_earnings(control, tickers, DAY)
    # only earnings docs: the arm's Scalp queue never gets control's news
    assert {r[0] for r in arm.execute("SELECT DISTINCT source FROM raw_docs")} == {"earnings"}


def test_copied_earnings_docs_keep_ids_and_reset_scalp_bookkeeping(
    control: sqlite3.Connection,
) -> None:
    with control:
        control.execute(
            "UPDATE raw_docs SET scalp_status = 'scouted', scalp_run_id = 'run-c' "
            "WHERE url LIKE '%/UNH/%'"
        )
    arm = _db()
    sync_reference_tables(control, arm, as_of=CHAIN)
    rows = {
        r["url"].split("/")[-2]: r
        for r in arm.execute("SELECT url, scalped_at, scalp_run_id, scalp_status FROM raw_docs")
    }
    assert rows["UNH"]["scalp_status"] is None and rows["UNH"]["scalped_at"] is None
    assert rows["MU"]["scalp_status"] == FILTERED_STATUS and rows["MU"]["scalped_at"]
    assert all(r["scalp_run_id"] is None for r in rows.values())
    ids = "SELECT id FROM raw_docs WHERE source = 'earnings' AND ingested_at <= ? ORDER BY id"
    cut = (CHAIN.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),)
    assert arm.execute(ids, cut).fetchall() == control.execute(ids, cut).fetchall()


def test_iv_parity(control: sqlite3.Connection) -> None:
    arm = _db()
    ivs = safe_store(arm)
    assert ivs is not None and ivs.series("MU", until=DAY) == {}
    synced = sync_reference_tables(control, arm, as_of=CHAIN)
    assert synced["iv_daily"] == 200 and synced["iv_skips"] == 1
    a, c = safe_store(arm), safe_store(control)
    assert a is not None and c is not None
    assert a.series("MU", until=DAY) == c.series("MU", until=DAY)
    assert iv_rank(a.series("MU", until=DAY), DAY, 0.5) == iv_rank(
        c.series("MU", until=DAY), DAY, 0.5
    )
    assert a.days("MU", BACKFILL, include_skips=True) == c.days("MU", BACKFILL, include_skips=True)
    assert DAY + dt.timedelta(days=1) not in a.series("MU")  # control's later row stays out


def test_sync_is_idempotent_and_follows_control_rewrites(control: sqlite3.Connection) -> None:
    arm = _db()
    first = sync_reference_tables(control, arm, as_of=CHAIN)
    assert sum(first.values()) == 3 + 200 + 1 + 3
    assert sync_reference_tables(control, arm, as_of=CHAIN) == dict.fromkeys(first, 0)
    # control's betas job rewrites today's MU row (a later refresh): the arm follows
    upsert_betas(control, [_beta("MU", DAY, 3.4)], now=AFTER)
    assert sync_reference_tables(control, arm, as_of=CHAIN)["betas"] == 1
    assert betas_used(arm, ["MU"], DAY)["MU"].raw == pytest.approx(3.4)
    assert sync_reference_tables(control, arm, as_of=CHAIN) == dict.fromkeys(first, 0)


def test_sync_never_writes_control_and_keeps_arm_only_rows(
    control: sqlite3.Connection, tmp_path: Path
) -> None:
    ro = _ro(control, tmp_path)  # a read-only control works (the arm tick opens it mode=ro)
    arm = _db()
    upsert_betas(arm, [_beta("ZZ", DAY, 1.5)], now=BEFORE)
    sync_reference_tables(ro, arm, as_of=CHAIN)
    assert arm.execute("SELECT count(*) FROM betas WHERE ticker = 'ZZ'").fetchone()[0] == 1


def test_old_store_without_a_table_is_skipped(control: sqlite3.Connection) -> None:
    arm = _db()
    arm.execute("DROP TABLE betas")
    assert sync_reference_tables(control, arm, as_of=CHAIN)["betas"] == 0


# --- the exhaustive-list guard -----------------------------------------------------

# Packages whose code runs in an arm's forked chain or its own jobs, read straight from
# the store (the card names pipeline / gate / betas; iv, positions, exits, broker,
# execution and scanner run there too).
SCANNED = (
    "arc/pipeline",
    "arc/gate",
    "arc/betas",
    "arc/iv",
    "arc/positions",
    "arc/exits",
    "arc/broker",
    "arc/execution",
    "arc/scanner",
)
_FROM = re.compile(r"\b(?:FROM|JOIN)\s+([a-z_][a-z0-9_]*)\b")


def _tables_read() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for pkg in SCANNED:
        for path in sorted((ROOT / pkg).rglob("*.py")):
            for table in _FROM.findall(path.read_text()):
                found.setdefault(table, set()).add(str(path.relative_to(ROOT)))
    return found


def test_every_table_the_arm_reads_is_classified() -> None:
    """A new ``FROM <table>`` in the scanned packages must be listed as a control
    reference table (synced) or as per-arm, so no table silently goes missing."""
    schema = {r[0] for r in _db().execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    reference = {t.name for t in ARM_REFERENCE_TABLES}
    unclassified = {
        t: sorted(files)
        for t, files in _tables_read().items()
        if t in schema and t not in reference and t not in ARM_OWN_TABLES
    }
    assert unclassified == {}, (
        "add each to arc.experiments.reference.ARM_REFERENCE_TABLES (control-owned, synced) "
        f"or ARM_OWN_TABLES (per arm): {unclassified}"
    )


def test_reference_and_own_lists_are_disjoint_real_tables() -> None:
    schema = {r[0] for r in _db().execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    reference = [t.name for t in ARM_REFERENCE_TABLES]
    assert len(reference) == len(set(reference))
    assert not set(reference) & ARM_OWN_TABLES
    assert set(reference) <= schema and schema >= ARM_OWN_TABLES
    conn = _db()
    for spec in ARM_REFERENCE_TABLES:  # the natural key is the table's primary key
        pk = [
            r[1]
            for r in sorted(conn.execute(f"PRAGMA table_info({spec.name})"), key=lambda r: r[5])
            if r[5]
        ]  # noqa: E501
        assert tuple(pk) == spec.key, spec.name


def test_guard_catches_the_live_gap() -> None:
    """The three tables of the live incident are read by the scanned code."""
    read = _tables_read()
    assert {"betas", "iv_daily", "raw_docs"} <= set(read)
