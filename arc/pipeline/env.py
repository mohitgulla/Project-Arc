"""What the pipeline steps talk to: market data, the account, and the persona LLMs.

Two builders:

* :meth:`PipelineEnv.live`: Alpaca market data, the Alpaca **paper** account
  (read-only: ``account()``/``positions()``; nothing here submits orders), and
  Hermes for Research/Quant/Risk, each on the model its tier in
  ``config/llm_routing.yaml`` names (PLAN §2.4, E8.1). Only a
  live run with the broker (not --dry-run) mints gate tokens.
* :meth:`PipelineEnv.fixtures`: fully offline. It uses the recorded SPY, NVDA, XOM,
  PLTR and UFPT chains (E5.7), a fixture symbol master, a fixed paper account and
  canned persona responses. ``arc propose --fixtures`` uses it, and so do the tests.

Personas only ever see prompt text. The runner (deterministic code) is what
reads the account and the chains, so AGENTS.md's "personas never call the
broker" rule holds.
"""

from __future__ import annotations

import datetime as _dt
import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from arc.broker.base import AccountInfo, BrokerPosition
from arc.ingest.llm import FixtureScalpLLM, HermesScalpLLM
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

    from arc.broker.base import BrokerAdapter
    from arc.config import ArcSettings
    from arc.data.base import MarketDataProvider
    from arc.ingest.llm import PersonaLLM
    from arc.universe.guard import UniverseGuard

__all__ = ["FIXTURES_DIR", "FIXTURE_NOW", "FIXTURE_SETS", "PERSONAS", "PipelineEnv"]

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
PERSONAS = ("research", "quant", "risk")
# Canned persona reply sets: "neutral" (SPY iron condor; the default) and
# "bullish" (SPY bull call debit, for the D25 cash_debit profile).
FIXTURE_SETS: dict[str, Path] = {"neutral": FIXTURES_DIR, "bullish": FIXTURES_DIR / "bullish"}

# The recorded SPY chain holds the 2026-09-25 close (quotes stamped 15:59:17-15:59:59
# ET). Offline runs happen "at" that close so quote-freshness and DTE checks mean
# what they mean live.
FIXTURE_NOW = _dt.datetime(2026, 9, 25, 16, 0, 0, tzinfo=ET)


@dataclass
class PipelineEnv:
    """Injected dependencies of the E5.2 steps (one per ``arc propose`` / tick)."""

    market: MarketDataProvider
    account: Callable[[], AccountInfo]
    positions: Callable[[], list[BrokerPosition]]
    llms: dict[str, PersonaLLM]
    scalp_llm: PersonaLLM | None = None  # None = run_scalp's own default (Hermes cheap tier)
    offline: bool = False
    mint_tokens: bool = False  # issue gate tokens on PASS (live paper runs only)
    notes: list[str] = field(default_factory=list)
    # D32: the broker whose order list cross-checks the daily order budget (live only).
    broker: BrokerAdapter | None = None
    # D28: builds the Scalp's universe guard (None = from settings, live Alpaca data).
    # Fixtures pass one backed by the recordings and a fixture symbol master.
    universe_guard: Callable[[ArcSettings, _dt.datetime], UniverseGuard] | None = None
    # E5.9 / D33: a VIX reading for the market-conditions guard when no `vol_term`
    # context entry is fresh. None = try the market provider's index quote. Fixtures
    # return the bundled calm reading (arc/pipeline/fixtures/vix.json), so offline runs
    # exercise the guard instead of failing closed on missing data.
    vix_quote: Callable[[], tuple[float, str] | None] | None = None

    def llm(self, persona: str) -> PersonaLLM:
        return self.llms[persona]

    # -- builders ------------------------------------------------------------

    @classmethod
    def live(
        cls,
        settings: ArcSettings,
        *,
        broker: bool = True,
        conn: sqlite3.Connection | None = None,
        chain_run_id: str | None = None,
    ) -> PipelineEnv:
        """Alpaca data and Hermes personas. With ``broker=False`` (dry run) the
        account is the fixture account and no broker client is built.

        E10.2 (D44): with the store *conn* the broker is the store's
        (:func:`arc.experiments.broker.trading_broker`: an experiment arm store
        trades only its own paper account, through its virtual account) and the
        market data is taped for a paired arm (:func:`arc.experiments.tape.tape_market`:
        control's loop records its reads, the arm's paired chain replays them).
        """
        from arc.data.alpaca import AlpacaMarketData

        # Each persona's model comes from config/llm_routing.yaml (E8.1).
        llms: dict[str, PersonaLLM] = {
            p: HermesScalpLLM.from_settings(
                settings, p, timeout_seconds=settings.persona_timeout_seconds
            )
            for p in PERSONAS
        }
        if broker:
            if conn is not None:
                from arc.experiments.broker import trading_broker

                paper: BrokerAdapter = trading_broker(conn, settings)
            else:
                from arc.broker.alpaca_paper import AlpacaPaperBroker

                paper = AlpacaPaperBroker()
            account: Callable[[], AccountInfo] = paper.account
            positions: Callable[[], list[BrokerPosition]] = paper.positions
            adapter: BrokerAdapter | None = paper
        else:
            account, positions, adapter = fixture_account, list, None
        market: MarketDataProvider = AlpacaMarketData()
        if conn is not None:
            from arc.experiments.tape import tape_market

            market = tape_market(conn, chain_run_id, market)
        return cls(
            market=market,
            account=account,
            positions=positions,
            llms=llms,
            mint_tokens=broker,
            broker=adapter,
        )

    @classmethod
    def fixtures(cls, directory: Path | None = None) -> PipelineEnv:
        """Offline: recorded chains, fixture account, canned Scalp + persona replies.

        The market is the E5.7 multi-name recording set (SPY, NVDA, XOM, PLTR, UFPT);
        the Scalp's universe guard uses a fixture symbol master and that same market,
        so the liquidity screen runs offline on recorded data.
        """
        from arc.data.recorded import MULTI_NAME_FIXTURES, RecordedMarketData
        from arc.ingest.scalp import FIXTURES_DIR as SCALP_FIXTURES

        directory = directory or FIXTURES_DIR
        llms: dict[str, PersonaLLM] = {
            p: FixtureScalpLLM([(directory / f"{p}.json").read_text()]) for p in PERSONAS
        }
        market = RecordedMarketData.from_files(*MULTI_NAME_FIXTURES)
        return cls(
            market=market,
            account=fixture_account,
            positions=list,
            llms=llms,
            scalp_llm=FixtureScalpLLM.from_dir(SCALP_FIXTURES / "responses"),
            offline=True,
            universe_guard=lambda s, now: fixture_universe_guard(s, now, market),
            vix_quote=fixture_vix,
        )


def fixture_vix() -> tuple[float, str]:
    """The bundled VIX reading offline runs feed the D33 market guard."""
    data = json.loads((FIXTURES_DIR / "vix.json").read_text())
    return float(data["vix"]), str(data["as_of"])


# D51: names the recorded fixture day treats as seeds beyond today's core (see below).
FIXTURE_SEED_EXTRA: frozenset[str] = frozenset({"SPY"})


def fixture_universe_guard(
    settings: ArcSettings, now: _dt.datetime, market: MarketDataProvider
) -> UniverseGuard:
    """Offline universe guard: fixture symbol master + recorded market data."""
    from arc.universe.guard import UniverseGuard
    from arc.universe.master import SymbolMaster

    master = SymbolMaster.model_validate_json((FIXTURES_DIR / "symbol_master.json").read_text())
    guard = UniverseGuard.from_settings(
        settings, now=now, master=master, market_factory=lambda: market
    )
    # D51: SPY left the core list, but the recorded fixture day (2026-09-25) was
    # captured with SPY in the D9 seed list and its recording has no daily volume
    # (pre-E5.7), so it cannot be screened. Keep it a seed here, as on that day.
    guard.seed = guard.seed | FIXTURE_SEED_EXTRA
    return guard


def fixture_account() -> AccountInfo:
    """The paper account used by dry runs and fixtures (no broker call)."""
    data = json.loads((FIXTURES_DIR / "account.json").read_text())
    return AccountInfo(
        account_id=data["account_id"],
        equity=Decimal(data["equity"]),
        buying_power=Decimal(data["buying_power"]),
        cash=Decimal(data["cash"]),
        last_equity=Decimal(data["last_equity"]),
    )
