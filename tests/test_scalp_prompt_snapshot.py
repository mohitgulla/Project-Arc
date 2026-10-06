"""E13.10: the flag-off Scalp stage-2 prompt is byte-identical to the pre-card prompt."""

from __future__ import annotations

from pathlib import Path

from arc.context.kinds import ChainSnapshotPayload, IndexVol, IndexVolsPayload
from arc.ingest.cboe_fast import scalp_tape
from arc.ingest.scalp import build_stage2_prompt
from arc.utils.calendar import ET
from tests import scalp_prompt_golden as golden

REPO = Path(__file__).resolve().parents[1]
GOLDEN = REPO / "tests" / "fixtures" / "scalp" / "stage2_prompt_flag_off.txt"


def test_flag_off_prompt_matches_main_golden() -> None:
    assert golden.prompt() == GOLDEN.read_text()


def test_flag_on_prompt_only_adds_the_tape_block() -> None:
    import datetime as dt

    now = dt.datetime(2026, 10, 6, 10, 0, tzinfo=ET)
    fetched = now.isoformat()
    iv = IndexVolsPayload(
        fetched_at=fetched, quotes=[IndexVol(symbol="VIX", value=17.6, as_of=fetched)]
    )
    snap = ChainSnapshotPayload(
        ticker="NVDA",
        fetched_at=fetched,
        spot=180.0,
        expiry="2026-11-20",
        call_volume_td=1000,
        put_volume_td=500,
        put_call_volume=0.5,
        atm_spread_pct=0.01,
        atm_oi=900,
        book=[],
    )
    tape = scalp_tape(iv, [snap], None, None, now=now, max_age=dt.timedelta(minutes=30))
    on = build_stage2_prompt(
        golden.digests(),
        golden.settings(),
        golden.DAY,
        open_universe=True,
        ticker_facts="",
        universe=["NVDA", "AAPL", "SPY"],
        tape=tape.text,
    )
    off = GOLDEN.read_text()
    head, _, rest = on.partition("\n## Options tape (Cboe, code-built)\n")
    assert rest, "tape block missing"
    block, _, tail = rest.partition("\n\n## Output format")
    assert head + "\n## Output format" + tail == off
    assert "NVDA" in block and len(tape.text) <= 1500


def test_xp8_draft_spec_and_flag_registry() -> None:
    from arc.control.registry import lookup
    from arc.experiments.overlay import load_spec
    from arc.routines.config import PERSONA_FLAGS, load_routines

    spec = load_spec(REPO / "config/experiments/live/xp8_scalp_options_tape.yaml")
    assert spec.id == "XP-8"
    assert spec.arms.treatment.overlay == {"routines": {"personas": {"scalp_options_tape": "on"}}}
    assert "scalp_options_tape" in PERSONA_FLAGS
    t = lookup("scalp_options_tape")
    assert t.key == "personas.scalp_options_tape" and t.choices == ("off", "on")
    assert load_routines(REPO / "config/routines.yaml").scalp_options_tape.enabled is False
