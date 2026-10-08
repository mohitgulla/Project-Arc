"""E13.13 (D56): emoji persona labels, note v5 sections, Research Opens/Exits, exit cards."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from arc.context.kinds import (
    NOTE_SECTION_MAX,
    NOTE_SECTION_ORDER,
    NotePayload,
    NoteSection,
    NoteTopic,
)
from arc.context.render import fact_text, note_lines, render_note_text
from arc.personas.schemas import ExitWatchItem, ResearchOutput, ResearchRankedItem, RiskExitVerdict
from arc.positions.exit_case import ExitCase, ExitCaseFacts, ExitTrigger
from arc.routines.heartbeat import _PERSONA_LABELS
from arc.slack import blocks as B
from arc.slack import digests as D
from arc.slack.personas import PERSONA_EMOJI, PERSONA_KEYS, Persona, persona_for, persona_label
from arc.utils.calendar import ET

WEB = Path(__file__).resolve().parents[1] / "web" / "src" / "lib" / "performance.ts"


def _text(blocks: list[dict[str, Any]]) -> str:
    return json.dumps(blocks, ensure_ascii=False)


def _limits(view: B.CardView) -> None:
    assert len(view.blocks) <= 50
    for b in view.blocks:
        t = b.get("text")
        if isinstance(t, dict):
            assert len(t["text"]) <= 3000


# -- labels ------------------------------------------------------------------


def test_every_persona_has_one_emoji_label() -> None:
    assert set(PERSONA_EMOJI) == set(Persona)
    assert persona_label(Persona.SCOUT) == "🔭 [Scout]"
    assert persona_label(Persona.RESEARCH) == "🧠 [Research]"
    assert persona_label(Persona.QUANT, suffix=" (revised)") == "🤺 [Quant (revised)]"
    assert persona_for("director") is Persona.RESEARCH
    assert persona_for("investor") is Persona.BROKER
    assert persona_for("nope") is None


def test_heartbeat_labels_come_from_the_persona_map() -> None:
    for key, label in _PERSONA_LABELS.items():
        assert label == persona_label(PERSONA_KEYS[key])
    assert "director" not in _PERSONA_LABELS  # legacy aliases are not separate labels


def test_tower_serves_the_python_emoji_map() -> None:
    # E13.14: the SPA keeps no emoji mirror; it reads /api/meta, built from this map.
    from arc.routines.config import load_routines
    from arc.tower.catalogue import persona_catalogue

    served = {p.key: p.emoji for p in persona_catalogue(load_routines()) if p.emoji}
    assert served == {p.value.lower(): e for p, e in PERSONA_EMOJI.items()}
    assert "PERSONA_EMOJI" not in WEB.read_text()


# -- note v5 -----------------------------------------------------------------


def _note(**kw: Any) -> NotePayload:
    base: dict[str, Any] = {
        "persona": "research",
        "topic": NoteTopic.THESIS,
        "title": "SPY neutral thesis",
    }
    return NotePayload(**{**base, **kw})


def test_note_needs_body_or_sections() -> None:
    with pytest.raises(ValidationError):
        _note()
    _note(body="legacy")
    _note(sections=[NoteSection(label="Thesis", text="x")])


def test_note_rejects_unknown_label_and_long_section() -> None:
    with pytest.raises(ValidationError):
        NoteSection(label="Vibes", text="x")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        NoteSection(label="Thesis", text="x" * (NOTE_SECTION_MAX + 1))


def test_sections_render_in_fixed_order_and_missing_are_omitted() -> None:
    note = _note(
        sections=[
            NoteSection(label="Risks", text="FOMC."),
            NoteSection(label="Thesis", text="Range-bound."),
        ],
        facts={"vix": 17.625, "pool": 23, "guard": True},
    )
    assert render_note_text(note) == (
        "Thesis: Range-bound.\nRisks: FOMC.\nFacts: vix 17.625 · pool 23 · guard yes"
    )
    assert "n/a" not in render_note_text(note) and "Regime" not in render_note_text(note)


def test_legacy_body_note_renders_unchanged() -> None:
    note = _note(body="intact: still fine.")
    assert note_lines(note) == [(None, "intact: still fine.")]
    assert render_note_text(note) == "intact: still fine."
    blocks = B.render_note(note)
    assert blocks[0]["text"]["text"] == "*🧠 [Research] SPY neutral thesis*\nintact: still fine."


_label = st.sampled_from(NOTE_SECTION_ORDER)
_txt = st.text(alphabet=st.characters(blacklist_categories=("Cs", "Cc")), min_size=1, max_size=60)


@given(
    st.lists(st.tuples(_label, _txt), min_size=1, max_size=8, unique_by=lambda p: p[0]),
    st.dictionaries(
        st.from_regex(r"[a-z]{1,8}", fullmatch=True),
        st.one_of(st.integers(), st.floats(allow_nan=False), st.booleans(), _txt),
        max_size=5,
    ),
)
def test_slack_text_equals_tower_text_modulo_markup(
    pairs: list[tuple[str, str]], facts: dict[str, Any]
) -> None:
    secs = [NoteSection(label=lb, text=t) for lb, t in pairs if t.strip()]  # type: ignore[arg-type]
    if not secs:
        return
    note = _note(sections=secs, facts=facts)
    blocks = B.render_note(note)
    slack = blocks[0]["text"]["text"].split("\n", 1)[1]
    plain = slack.replace("*", "")
    for a, b in (("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&")):
        plain = plain.replace(a, b)
    tower = render_note_text(note)
    tower_body = tower.rsplit("\nFacts: ", 1)[0] if facts else tower
    assert plain == tower_body.replace("*", "")
    for v in facts.values():
        assert fact_text(v) == (str(v) if not isinstance(v, bool) else ("yes" if v else "no"))


def test_float_facts_are_never_rounded() -> None:
    assert fact_text(0.123456789) == "0.123456789"


# -- Research card -----------------------------------------------------------


def _ranked(t: str = "SPY", rank: int = 1) -> ResearchRankedItem:
    return ResearchRankedItem(
        ticker=t,
        rank=rank,
        stance="neutral",
        confidence=0.7,
        thesis="Range-bound into FOMC.",
        regime_context="Low realised vol.",
        suggested_structure_type="iron_condor",
        evidence=["8-K"],
    )


def _out(**kw: Any) -> ResearchOutput:
    base: dict[str, Any] = {
        "shortlist": [_ranked(), _ranked("NVDA", 2)],
        "market_regime": "risk_on",
        "session_notes": "Two setups.",
        "excluded": [{"ticker": "XOM", "reason": "Crude already priced."}],
    }
    return ResearchOutput(**{**base, **kw})


def _watch(t: str, action: str = "hold", status: str = "intact") -> ExitWatchItem:
    return ExitWatchItem(
        structure_id=f"os-{t.lower()}",
        ticker=t,
        action=action,  # type: ignore[arg-type]
        thesis_status=status,  # type: ignore[arg-type]
        evidence=[f"{t} catalyst still live"],
        reason="Still working.",
    )


def test_research_card_opens_without_exits_and_no_excluded_list() -> None:
    view = D.research_card(
        _out(), candidates=5, funnel=[("XOM", "excluded", "Crude already priced.")], budget=1
    )
    text = _text(view.blocks)
    assert view.text.startswith("🧠 [Research] Ranked: 2 / 5")
    assert "*Opens:* 1 ranked within budget · 1 beyond the budget of 1" in text
    assert "*Thesis:* Range-bound into FOMC." in text and "*Regime:* Low realised vol." in text
    assert "Exits" not in text  # exit_path deterministic: no section at all
    assert "Excluded" not in text and "XOM" not in text
    _limits(view)


def test_research_card_exits_section() -> None:
    items = [_watch("CRWD", "review", "weakened"), *[_watch(f"T{i}") for i in range(13)]]
    view = D.research_card(_out(), candidates=5, exits=items)
    text = _text(view.blocks)
    assert "*Exits (14):*" in text
    assert "• *CRWD* · review · weakened · CRWD catalyst still live" in text  # review first
    assert "+2 more" in text  # ≤ 12 lines
    assert "14 watched · 1 review" in text
    empty = _text(D.research_card(_out(), candidates=5, exits=[]).blocks)
    assert "*Exits:* none open" in empty
    _limits(view)


def test_research_card_no_trade_opens_line() -> None:
    view = D.research_card(_out(shortlist=[], no_trade_reason="unclear"), candidates=3)
    assert "*Opens:* nothing worth trading today (Unclear)" in _text(view.blocks)


# -- exit cards --------------------------------------------------------------


def _case(t: str = "CRWD", rec: str = "close") -> ExitCase:
    return ExitCase(
        structure_id=f"os-{t.lower()}",
        ticker=t,
        triggers=[ExitTrigger(kind="profit_target", detail="80% of max gain")],
        facts=ExitCaseFacts(
            dte=21,
            contracts=2,
            credit=True,
            pnl_total=170.0,
            stop_state="armed_eod",
            close_now_net=85.0,
            remaining_ev_hold=12.0,
            remaining_ev_managed=30.0,
            thesis_status="intact",
        ),
        recommendation=rec,  # type: ignore[arg-type]
        rationale="Most of the gain is banked.",
    )


def test_quant_exit_card_line_per_case() -> None:
    view = D.quant_exit_card([_case(), _case("MU", "hold")], shadow=True)
    text = _text(view.blocks)
    assert view.text == "🤺 [Quant] Exit cases: 2 judged • 1 close"
    assert (
        "*CRWD* `os-crwd` · Profit target · EV hold +$12.00 / managed +$30.00 · close now +$85.00"
        in text
    )
    assert "*Recommendation:* Close" in text and "*Rationale:* Most of the gain" in text
    assert "Shadow: journaled only" in text
    many = D.quant_exit_card([_case(f"T{i}") for i in range(10)])
    assert "+2 more: T8, T9" in _text(many.blocks)
    _limits(many)


def test_risk_exit_card_verdict_chip_and_reason_code() -> None:
    v = RiskExitVerdict(
        structure_id="os-crwd", verdict="close", reason_code="ev_exhausted", reason="Little left."
    )
    view = D.risk_exit_card([_case(), _case("MU", "hold")], {"os-crwd": v})
    text = _text(view.blocks)
    assert view.text == "🛡️ [Risk] Exit review: 2 reviewed • 1 close"
    assert "*CRWD* `os-crwd` · Quant close → *Close* · `ev_exhausted`" in text
    assert "*Reason:* Little left." in text and "MU" not in text


def test_exits_mandatory_summary() -> None:
    assert D.exits_mandatory_summary([]).startswith("no mandatory exit signals")
    assert (
        D.exits_mandatory_summary([("CRWD", "dte_exit", "ab12cd34ef"), ("MU", "stop", None)])
        == "mandatory exits: CRWD · dte exit · proposal ab12cd34; MU · stop · not proposed"
    )


# -- read-only render CLI ----------------------------------------------------


def test_slack_render_cli_reads_a_chain_without_writing(tmp_path: Path) -> None:
    from arc.cli import main
    from arc.context.store import ContextStore
    from arc.store.db import connect
    from arc.store.migrate import migrate

    db = tmp_path / "arc.db"
    conn = connect(db)
    migrate(conn)
    note = _note(sections=[NoteSection(label="Thesis", text="Range-bound.")], facts={"rank": 1})
    ContextStore(conn).write(
        kind="note",
        subject="SPY",
        payload=note,
        produced_by="research",
        run_id="r1",
        chain_run_id="ch1",
        now=dt.datetime(2026, 10, 6, 10, 0, tzinfo=ET),
    )
    conn.commit()
    before = conn.execute("SELECT count(*) FROM context_entries").fetchone()[0]
    conn.close()
    out = tmp_path / "cards.json"
    assert main(["slack", "render", "--db", str(db), "--chain", "ch1", "--out", str(out)]) == 0
    data = json.loads(out.read_text())
    assert data["chain"] == "ch1" and len(data["notes"]) == 1
    assert "*Thesis:* Range-bound." in _text(data["notes"][0]["blocks"])
    ro = sqlite3.connect(db)
    assert ro.execute("SELECT count(*) FROM context_entries").fetchone()[0] == before
    ro.close()
    assert main(["slack", "render", "--db", str(tmp_path / "nope.db"), "--chain", "x"]) == 2


# -- Tower: the Why tab reads the same note object ----------------------------


def test_tower_chain_notes_use_the_shared_renderer(tmp_path: Path) -> None:
    from arc.context.store import ContextStore
    from arc.store.db import connect, connect_ro
    from arc.store.migrate import migrate
    from arc.tower.data_trades import _chain_notes

    db = tmp_path / "arc.db"
    conn = connect(db)
    migrate(conn)
    store = ContextStore(conn)
    now = dt.datetime(2026, 10, 6, 10, 0, tzinfo=ET)
    v5 = _note(
        sections=[
            NoteSection(label="Regime", text="Low vol."),
            NoteSection(label="Thesis", text="Range-bound."),
        ],
        facts={"rank": 1},
    )
    for subject, payload, chain in (
        ("SPY", v5, "ch1"),
        ("SPY", _note(body="legacy body"), "ch1"),
        ("NVDA", v5, "ch1"),  # another ticker: not this trade's
        ("SPY", v5, "ch2"),  # another chain
    ):
        store.write(
            kind="note",
            subject=subject,
            payload=payload,
            produced_by="research",
            run_id="r",
            chain_run_id=chain,
            supersede="accumulate",
            now=now,
        )
    conn.commit()
    conn.close()
    ro = connect_ro(db)
    notes = _chain_notes(ro, "ch1", "SPY", {})
    assert [n.subject for n in notes] == ["SPY", "SPY"]
    new, old = sorted(notes, key=lambda n: n.sections[0].label is None)  # same instant
    assert [(s.label, s.text) for s in new.sections] == [
        ("Thesis", "Range-bound."),
        ("Regime", "Low vol."),
    ]
    assert new.text == render_note_text(v5) and new.facts == {"rank": 1}
    assert old.sections[0].label is None and old.text == "legacy body"
    assert _chain_notes(ro, None, "SPY", {}) == []
    ro.close()


def test_scout_card_renders_the_read_sections_verbatim() -> None:
    from arc.context.kinds import ScoutReadPayload
    from arc.slack.digests import scout_card

    read = ScoutReadPayload(
        as_of="2026-10-06T07:00:00-04:00",
        session="2026-10-06",
        regime="Mild risk-on.",
        options_sentiment="Put/call 0.59.",
        themes=["AI capex", "Rates"],
        risks=["FOMC minutes Oct 7"],
        ticker_calls=[],
        inputs={
            "youtube_macro": {"present": 2, "configured": 2},
            "youtube_micro": {"present": 3, "configured": 3},
            "options_daily": "2026-10-05",
            "vx_curve": "2026-10-05",
            "vol_term": "2026-10-05",
        },
        discovery=["LW", "APLD"],
        discovery_fill=2,
        prompt_sha="x",
        model="m",
    )
    card = scout_card(read=read, max_discovery=20, min_discovery_alert=0, candidates=2)
    assert card.text == "🔭 [Scout] Daily read: Discovery 2/20"
    texts = [b["text"]["text"] for b in card.blocks if isinstance(b.get("text"), dict)]
    assert (
        "*Regime:* Mild risk-on.\n*Options sentiment:* Put/call 0.59.\n"
        "*Themes:* AI capex · Rates\n*Discovery (2/20):* LW, APLD\n*Risks:* FOMC minutes Oct 7"
    ) in texts
