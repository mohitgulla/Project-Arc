"""Read side of the decision journal: ``show`` tree, ``gaps`` report, ``replay`` check.

Everything here is read-only and deterministic. ``gaps`` shadow-prices rejected
structures from the E7.1 EOD option history when it is cached locally and
reports ``n/a`` otherwise.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from arc.journal.attribution import MULTIPLIER, calibration
from arc.journal.models import OutcomeStatus
from arc.journal.reasons import STAGE_ORDER, Choice, ReasonCode, Stage
from arc.journal.scorecard import calibration_points
from arc.journal.store import JournalStore
from arc.structures import parse_occ
from arc.utils.calendar import ET

if TYPE_CHECKING:
    import datetime as _dt
    import sqlite3

    from arc.journal.attribution import CalibrationBucket
    from arc.journal.models import DecisionRecord, OutcomeRecord

__all__ = [
    "GapsReport",
    "ReplayResult",
    "ShadowPricer",
    "gaps",
    "replay",
    "show_lines",
]

_SEP = " • "


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def _fmt_decision(d: DecisionRecord) -> str:
    parts = [f"[{d.persona.value.capitalize()}] {d.subject}", f"{d.choice}", f"{d.reason_code}"]
    if d.confidence is not None:
        parts.append(f"conf {d.confidence:.0%}")
    if d.proposal_hash:
        parts.append(f"proposal {d.proposal_hash[:12]}")
    line = _SEP.join(parts)
    extra = _payload_summary(d)
    if extra:
        line += f"{_SEP}{extra}"
    if d.reason_text:
        text = " ".join(d.reason_text.split())
        line += f"\n      “{text[:240]}{'…' if len(text) > 240 else ''}”"
    ids = [f"id {d.id}"]
    if d.persona_call_id:
        ids.append(f"call {d.persona_call_id}")
    if d.inputs_snapshot_id:
        ids.append(f"snapshot {d.inputs_snapshot_id[:17]}")
    if d.supersedes_id:
        ids.append(f"supersedes {d.supersedes_id}")
    return line + "\n      " + " · ".join(ids)


def _strikes(legs: list[dict[str, Any]]) -> str:
    out = []
    for leg in legs:
        try:
            occ = parse_occ(str(leg["occ_symbol"]))
        except (KeyError, ValueError):
            continue
        side = "+" if str(leg.get("side", "")).lower() == "long" else "-"
        out.append(f"{side}{occ.strike.normalize():f}{occ.kind.name[0]}")
    return "/".join(out)


def _payload_summary(d: DecisionRecord) -> str:
    p = d.payload
    if d.stage is Stage.STRUCTURE and "legs" in p:
        bits = [_strikes(p["legs"])]
        if p.get("net_debit_credit") is not None:
            bits.append(f"net {float(p['net_debit_credit']):+.2f}")
        if p.get("pop") is not None:
            bits.append(f"PoP {float(p['pop']):.0%}")
        if p.get("ev_per_contract") is not None:
            bits.append(f"EV ${float(p['ev_per_contract']):,.2f}")
        return " ".join(bits)
    if d.stage is Stage.SIZING and "contracts" in p:
        return (
            f"x{p['contracts']} (Risk {p.get('suggestion')}, cap {p.get('cap_contracts')}) "
            f"max loss ${float(p.get('max_loss_total') or 0):,.2f}"
        )
    if d.stage is Stage.RISK_REVIEW and "sizing_suggestion" in p:
        return (
            f"rating {p.get('risk_rating')} · suggests {p.get('sizing_suggestion')}"
            f" (cap {p.get('cap_contracts')})"
        )
    if d.stage is Stage.SHORTLIST and "stance" in p:
        return f"rank {p.get('rank')} {p.get('stance')} {p.get('suggested_structure_type')}"
    if d.stage is Stage.CANDIDATE and "stance" in p:
        return f"{p.get('stance')} {p.get('catalyst_type', '')}".strip()
    return ""


def _fmt_outcome(o: OutcomeRecord) -> str:
    parts = [f"{o.status}"]
    if o.entry_fill is not None:
        parts.append(f"fill {o.entry_fill:+} vs limit {o.limit_price:+}")
    if o.slippage_usd is not None:
        bps = f" ({o.slippage_bps:.0f} bps)" if o.slippage_bps is not None else ""
        parts.append(f"slippage ${o.slippage_usd:,.2f}{bps} vs cost {o.cost_bps:.0f} bps")
    if o.realised_pnl is not None:
        parts.append(f"P&L ${o.realised_pnl:,.2f} vs EV ${o.ev_total:,.2f}")
    if o.hold_to_expiry_shadow_pnl is not None:
        parts.append(f"hold-to-expiry ${o.hold_to_expiry_shadow_pnl:,.2f}")
    if o.exit_reason:
        parts.append(f"exit {o.exit_reason}")
    return _SEP.join(parts)


def show_lines(conn: sqlite3.Connection, ref: str) -> list[str]:
    """The full decision tree for a proposal hash (or prefix) or a ``chain-…`` id."""
    j = JournalStore(conn)
    chain, hashes = j.resolve(ref)
    if chain is not None:
        decisions = j.decisions(chain_run_id=chain)
        # approval-stage rows written after the chain carry the chain id too
    else:
        decisions = [d for h in hashes for d in j.decisions(proposal_hash=h)]
    head = f"decision journal · chain {chain or 'n/a'}"
    if hashes:
        head += f" · proposals {', '.join(h[:12] for h in hashes)}"
    out = [head]
    by_stage: dict[Stage, list[DecisionRecord]] = {}
    for d in decisions:
        by_stage.setdefault(d.stage, []).append(d)
    for stage in STAGE_ORDER:
        rows = by_stage.get(stage)
        if not rows:
            continue
        out.append(f"── {stage.value} ({len(rows)})")
        out.extend(f"  {_fmt_decision(d)}" for d in rows)
    calls = j.persona_calls(chain) if chain else []
    if calls:
        out.append(f"── persona calls ({len(calls)})")
        for c in calls:
            usage = []
            if c.get("input_tokens") is not None:
                usage.append(f"in {c['input_tokens']} / out {c.get('output_tokens')} tok")
            if c.get("latency_ms") is not None:
                usage.append(f"{c['latency_ms']} ms")
            if c.get("cost_usd") is not None:
                usage.append(f"${c['cost_usd']:.4f}")
            out.append(
                f"  [{c['persona'].capitalize()}] {c['status']} · model {c['model']} · "
                f"prompt {c['prompt_sha256'][:12]} · dropped {c['dropped']}"
                + (f" · {' · '.join(usage)}" if usage else "")
                + f"\n      id {c['id']}"
            )
    for h in hashes:
        mc = j.market_context(h)
        if mc is not None:
            vol = [
                f"{k} {v:.1%}"
                for k, v in (("ATM IV", mc.atm_iv), ("IVR", mc.ivr), ("HV20", mc.hv20))
                if v is not None
            ]
            out.append(f"── market @ proposal {h[:12]}")
            spot = f"{mc.underlying_last:.2f}" if mc.underlying_last is not None else "n/a"
            out.append(
                f"  {mc.subject} last {spot} · regime {mc.regime or 'n/a'}"
                + (f" · {' · '.join(vol)}" if vol else "")
                + (
                    f" · quotes as of {mc.quotes_as_of.astimezone(ET):%Y-%m-%d %H:%M:%S %Z}"
                    if mc.quotes_as_of
                    else ""
                )
            )
            for q in mc.legs:
                out.append(f"    {q.occ_symbol} bid {q.bid} ask {q.ask} mid {q.mid}")
        o = j.outcome(h)
        out.append(f"── outcome {h[:12]}")
        out.append(f"  {_fmt_outcome(o) if o else 'none yet (E6.2/E6.3 fill it in)'}")
        reviews = j.reviews(proposal_hash=h)
        out.append(f"── review {h[:12]}")
        if not reviews:
            out.append("  none yet")
        for r in reviews:
            out.append(
                f"  [{r.reviewer.value.capitalize()}] {r.label} · root cause {r.root_cause}"
                f" · cites {', '.join(r.cites)}" + (f"\n      “{r.notes}”" if r.notes else "")
            )
    if len(out) == 1:
        out.append("  (no journal rows)")
    return out


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayResult:
    call_id: str
    persona: str
    ok: bool
    detail: str


def replay(conn: sqlite3.Connection, ref: str) -> list[ReplayResult]:
    """Rebuild each persona prompt of the chain from its recorded snapshot + inputs.

    The rebuilt prompt's sha256 must equal ``persona_calls.prompt_sha256``.
    Calls recorded before migration 009 (no ``prompt_inputs``) are reported as
    not replayable.
    """
    from arc.context.store import ContextStore
    from arc.pipeline.steps import PROMPT_BUILDERS, build_prompt
    from arc.pipeline.store import sha256

    j = JournalStore(conn)
    chain, _ = j.resolve(ref)
    if chain is None:
        msg = f"{ref!r} has no chain run to replay"
        raise LookupError(msg)
    results: list[ReplayResult] = []
    for c in j.persona_calls(chain):
        persona = str(c["persona"])
        if persona not in PROMPT_BUILDERS:
            continue
        if not c.get("prompt_inputs") or not c.get("snapshot_id"):
            results.append(
                ReplayResult(c["id"], persona, False, "not replayable: no recorded inputs")
            )
            continue
        try:
            snap = ContextStore(conn).load_snapshot(c["snapshot_id"])
            prompt = build_prompt(persona, snap, json.loads(c["prompt_inputs"]))
        except (KeyError, LookupError, ValueError, TypeError) as exc:
            results.append(ReplayResult(c["id"], persona, False, f"rebuild failed: {exc}"))
            continue
        got = sha256(prompt)
        want = str(c["prompt_sha256"])
        if got == want:
            results.append(ReplayResult(c["id"], persona, True, f"sha {got[:12]} matches"))
        else:
            results.append(
                ReplayResult(
                    c["id"], persona, False, f"MISMATCH: rebuilt {got[:12]} != recorded {want[:12]}"
                )
            )
    return results


# ---------------------------------------------------------------------------
# gaps
# ---------------------------------------------------------------------------


class ShadowPricer:
    """Marks structures at a later date from the E7.1 EOD option cache (``options_eod``).

    ``value(legs, after)`` → ``(date, per-share net)`` at the latest cached
    session on or after *after*, or ``None`` if any leg is missing.
    """

    def __init__(self, root: Path | str, provider: str = "alpaca") -> None:
        from arc.data.history.store import ParquetHistoryStore

        self.store = ParquetHistoryStore(root)
        self.provider = provider
        self._cache: dict[str, Any] = {}

    def available(self) -> bool:
        return self.store.root.is_dir()

    def _frame(self, underlying: str) -> Any:
        if underlying not in self._cache:
            self._cache[underlying] = self.store.read(self.provider, underlying)
        return self._cache[underlying]

    def value(self, legs: list[dict[str, Any]], after: _dt.date) -> tuple[_dt.date, Decimal] | None:
        if not legs:
            return None
        occs = [parse_occ(str(leg["occ_symbol"])) for leg in legs]
        df = self._frame(occs[0].root)
        if df.empty:
            return None
        df = df[(df["date"] >= after) & (df["date"] <= occs[0].expiration)]
        if df.empty:
            return None
        day = max(df["date"])
        rows = df[df["date"] == day].set_index("symbol")
        net = Decimal(0)
        for leg, occ in zip(legs, occs, strict=True):
            sym = occ.format().replace(" ", "")
            if sym not in rows.index:
                return None
            r = rows.loc[sym]
            bid, ask, close = r.get("bid"), r.get("ask"), r.get("close")
            if bid == bid and ask == ask and bid is not None and ask is not None and ask > 0:
                px = (float(bid) + float(ask)) / 2
            elif close == close and close is not None:
                px = float(close)
            else:
                return None
            sign = 1 if str(leg.get("side", "")).lower() == "long" else -1
            net += Decimal(str(round(px, 4))) * sign * int(leg.get("ratio", 1))
        return day, net


@dataclass
class AltComparison:
    subject: str
    chosen: str
    alternative: str
    chosen_pnl: Decimal | None
    alt_pnl: Decimal | None
    as_of: _dt.date | None

    @property
    def better_by(self) -> Decimal | None:
        if self.chosen_pnl is None or self.alt_pnl is None:
            return None
        return self.alt_pnl - self.chosen_pnl


@dataclass
class GapsReport:
    since: _dt.datetime | None
    decisions: int = 0
    no_trade: Counter[str] = field(default_factory=Counter)
    rejected: Counter[str] = field(default_factory=Counter)
    alternatives: list[AltComparison] = field(default_factory=list)
    shadow: str = "n/a"
    calibration: list[CalibrationBucket] = field(default_factory=list)
    realised: int = 0
    slippage: list[tuple[str, float | None, float | None]] = field(default_factory=list)
    root_causes: Counter[str] = field(default_factory=Counter)
    labels: Counter[str] = field(default_factory=Counter)

    def lines(self) -> list[str]:
        out = [
            f"decision journal gaps since {self.since:%Y-%m-%d}"
            if self.since
            else "decision journal gaps (all time)",
            f"decisions: {self.decisions}",
            "── no-trade decisions by reason",
            *(f"  {k}: {v}" for k, v in self.no_trade.most_common()),
            "── rejected alternatives by reason",
            *(f"  {k}: {v}" for k, v in self.rejected.most_common()),
            f"── rejected structures vs chosen (shadow-priced: {self.shadow})",
        ]
        if not self.alternatives:
            out.append("  none")
        for a in self.alternatives:
            if a.better_by is None:
                out.append(f"  {a.subject} {a.alternative} vs chosen {a.chosen}: n/a")
            else:
                flag = "  ← would have done better" if a.better_by > 0 else ""
                out.append(
                    f"  {a.subject} {a.alternative} ${a.alt_pnl:,.2f}/contract vs chosen "
                    f"{a.chosen} ${a.chosen_pnl:,.2f} (as of {a.as_of}){flag}"
                )
        out.append(f"── calibration (stated vs realised, {self.realised} realised trade(s))")
        if not self.calibration:
            out.append("  n/a: no closed trades yet")
        for b in self.calibration:
            out.append(
                f"  {b.persona:<9} [{b.lo:.1f}, {b.hi:.1f}) n={b.n} stated {b.stated_mean:.0%} "
                f"realised {b.hit_rate:.0%} gap {b.gap:+.0%}"
            )
        out.append("── entry slippage vs expected cost (bps of max loss)")
        if not self.slippage:
            out.append("  n/a: no fills yet")
        for h, slip, cost in self.slippage:
            s = f"{slip:.0f}" if slip is not None else "n/a"
            c = f"{cost:.0f}" if cost is not None else "n/a"
            out.append(f"  {h[:12]} slippage {s} vs cost {c}")
        for title, counts in (("review labels", self.labels), ("root causes", self.root_causes)):
            out.append(f"── {title}")
            out.extend(f"  {k}: {v}" for k, v in counts.most_common())
            if not counts:
                out.append("  none yet")
        return out


def _chosen_structures(decisions: list[DecisionRecord]) -> dict[tuple[str, str], DecisionRecord]:
    return {
        (d.chain_run_id or "", d.subject): d
        for d in decisions
        if d.stage is Stage.STRUCTURE and d.choice is Choice.SELECTED and "legs" in d.payload
    }


def _per_contract(entry: Any, mark: Decimal) -> Decimal:
    return (mark - Decimal(str(entry))) * MULTIPLIER


def gaps(
    conn: sqlite3.Connection,
    *,
    since: _dt.datetime | None = None,
    pricer: ShadowPricer | None = None,
) -> GapsReport:
    j = JournalStore(conn)
    decisions = j.decisions(since=since)
    rep = GapsReport(since=since, decisions=len(decisions))
    for d in decisions:
        if d.choice is Choice.NO_TRADE:
            rep.no_trade[str(d.reason_code)] += 1
        elif d.choice is Choice.REJECTED and d.reason_code is not ReasonCode.OWNER_REJECT:
            rep.rejected[f"{d.stage}:{d.reason_code}"] += 1

    chosen = _chosen_structures(decisions)
    usable = pricer is not None and pricer.available()
    rep.shadow = "EOD option history" if usable else "n/a (no E7.1 history cached)"
    for d in decisions:
        if not (
            d.stage is Stage.STRUCTURE
            and d.reason_code is ReasonCode.MENU_NOT_CHOSEN
            and "legs" in d.payload
        ):
            continue
        pick = chosen.get((d.chain_run_id or "", d.subject))
        comp = AltComparison(
            subject=d.subject,
            chosen=_strikes(pick.payload["legs"]) if pick else "none",
            alternative=_strikes(d.payload["legs"]),
            chosen_pnl=None,
            alt_pnl=None,
            as_of=None,
        )
        if usable and pricer is not None:
            after = d.at.date()
            alt = pricer.value(d.payload["legs"], after)
            if alt is not None:
                comp.alt_pnl = _per_contract(d.payload["net_debit_credit"], alt[1])
                comp.as_of = alt[0]
            if pick is not None:
                ch = pricer.value(pick.payload["legs"], after)
                if ch is not None:
                    comp.chosen_pnl = _per_contract(pick.payload["net_debit_credit"], ch[1])
                    comp.as_of = comp.as_of or ch[0]
        rep.alternatives.append(comp)

    realised = [
        o
        for o in j.outcomes(since=since)
        if o.status in (OutcomeStatus.CLOSED, OutcomeStatus.EXPIRED_WORTHLESS)
        and o.realised_pnl is not None
    ]
    rep.realised = len(realised)
    rep.calibration = calibration(
        calibration_points(
            conn, [(o.proposal_hash, bool(o.realised_pnl and o.realised_pnl > 0)) for o in realised]
        )
    )
    rep.slippage = [
        (o.proposal_hash, o.slippage_bps, o.cost_bps)
        for o in j.outcomes(since=since)
        if o.entry_fill is not None
    ]
    for r in j.reviews(since=since):
        rep.root_causes[str(r.root_cause)] += 1
        rep.labels[str(r.label)] += 1
    return rep


def default_pricer(data_dir: Path | str = Path("data")) -> ShadowPricer:
    return ShadowPricer(data_dir)
