"""What the pipeline steps talk to: market data, the account, and the persona LLMs.

Two builders:

* :meth:`PipelineEnv.live`: Alpaca market data, the Alpaca **paper** account
  (read-only: ``account()``/``positions()``; nothing here submits orders), and
  Hermes on the frontier tier for Director/Quant/Risk (PLAN §2.4).
* :meth:`PipelineEnv.fixtures`: fully offline. It uses the recorded SPY chain,
  a fixed paper account and canned persona responses. ``arc propose --fixtures``
  uses it, and so do the tests.

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
from arc.ingest.llm import FixtureScoutLLM, HermesScoutLLM
from arc.utils.calendar import ET

if TYPE_CHECKING:
    from collections.abc import Callable

    from arc.config import ArcSettings
    from arc.data.base import MarketDataProvider
    from arc.ingest.llm import ScoutLLM

__all__ = ["FIXTURES_DIR", "FIXTURE_NOW", "PERSONAS", "PipelineEnv"]

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
PERSONAS = ("director", "quant", "risk")

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
    llms: dict[str, ScoutLLM]
    scout_llm: ScoutLLM | None = None  # None = run_scout's own default (Hermes cheap tier)
    offline: bool = False
    iv_history_dir: Path | None = None
    notes: list[str] = field(default_factory=list)

    def llm(self, persona: str) -> ScoutLLM:
        return self.llms[persona]

    # -- builders ------------------------------------------------------------

    @classmethod
    def live(cls, settings: ArcSettings, *, broker: bool = True) -> PipelineEnv:
        """Alpaca data and Hermes personas. With ``broker=False`` (dry run) the
        account is the fixture account and no broker client is built."""
        from arc.data.alpaca import AlpacaMarketData

        llm = HermesScoutLLM(
            model=settings.persona_model,
            provider=settings.persona_provider,
            hermes_bin=settings.scout_hermes_bin,
            timeout_seconds=settings.persona_timeout_seconds,
        )
        if broker:
            from arc.broker.alpaca_paper import AlpacaPaperBroker

            paper = AlpacaPaperBroker()
            account: Callable[[], AccountInfo] = paper.account
            positions: Callable[[], list[BrokerPosition]] = paper.positions
        else:
            account, positions = fixture_account, list
        return cls(
            market=AlpacaMarketData(),
            account=account,
            positions=positions,
            llms=dict.fromkeys(PERSONAS, llm),
            iv_history_dir=settings.scanner_iv_history_dir,
        )

    @classmethod
    def fixtures(cls, directory: Path | None = None) -> PipelineEnv:
        """Offline: recorded chain, fixture account, canned Scout + persona replies."""
        from arc.data.recorded import SPY_CHAIN_FIXTURE, RecordedMarketData
        from arc.ingest.scout import FIXTURES_DIR as SCOUT_FIXTURES

        directory = directory or FIXTURES_DIR
        llms: dict[str, ScoutLLM] = {
            p: FixtureScoutLLM([(directory / f"{p}.json").read_text()]) for p in PERSONAS
        }
        return cls(
            market=RecordedMarketData.from_files(SPY_CHAIN_FIXTURE),
            account=fixture_account,
            positions=list,
            llms=llms,
            scout_llm=FixtureScoutLLM.from_dir(SCOUT_FIXTURES / "responses"),
            offline=True,
        )


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
