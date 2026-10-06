"""Weekly paper scorecard card (E7.3) for #arc-investor, in the shared E5.5 layout.

``[Ops] Scorecard: Sep 28 – Oct 02 • P&L +$312 • 5 Closed`` then a summary
line, a fact grid (one fact per line), the D19 early-exit and swap lines, the
D23 model-vs-realised numbers, slippage, gate violations, calibration and the
audit footer. Pure: a :class:`~arc.journal.scorecard.Scorecard` in, blocks out.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from arc.slack import blocks as B
from arc.slack.blocks import Block, CardView

if TYPE_CHECKING:
    from arc.journal.scorecard import Scorecard

__all__ = ["scorecard_card"]

_MAX_ROWS = 8


def _usd(v: float | None) -> str:
    if v is None:
        return "pending"
    sign = "-" if v < 0 else ("+" if v > 0 else "")
    return f"{sign}${abs(v):,.0f}"


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.0%}"


def _section(title: str, lines: list[str]) -> Block | None:
    if not lines:
        return None
    return {
        "type": "section",
        "text": {"type": "mrkdwn", "text": B.clip(f"*{title}*\n" + "\n".join(lines))},
    }


def _gate_fact(line: str) -> str:
    """E6.6a: the scorecard gate line, sentence case (``Scorecard gate: OFF …``)."""
    return line[:1].upper() + line[1:]


def _more(lines: list[str]) -> list[str]:
    if len(lines) <= _MAX_ROWS:
        return lines
    return [*lines[:_MAX_ROWS], f"…and {len(lines) - _MAX_ROWS} more (weekly report)"]


def scorecard_card(
    sc: Scorecard,
    *,
    report_path: str | None = None,
    run_id: str | None = None,
    chain_run_id: str | None = None,
) -> CardView:
    f, a, p, lim = sc.funnel, sc.funnel.approvals, sc.pnl, sc.order_budget
    title = (
        f"[Ops] Scorecard: {sc.label} • P&L {_usd(p.realised)} • {p.closed} Closed"
        f" • {f.fills} Fills"
    )
    summary = B.summary(
        f"Proposals {f.proposals}",
        f"Gate pass {f.gate_pass}/{f.gate_pass + f.gate_fail}",
        f"Approved {a.click_approved + a.auto_approved}",
        f"Win rate {_pct(p.win_rate)}",
        f"Equity {_usd(p.equity_change)}" if p.equity_change is not None else "",
    )
    peak = max((d.orders for d in sc.budget_days), default=0)
    tiers = Counter(d.tier for d in sc.budget_days if d.tier != "normal")
    grid = B.facts(
        [
            (
                "Funnel",
                f"Proposals {f.proposals} (open {f.proposals_open}, close {f.proposals_close})\n"
                f"Gate pass {f.gate_pass} · fail {f.gate_fail}\n"
                f"Fills {f.fills} ({f.contracts_filled} contracts)",
            ),
            (
                "Approvals",
                f"Click-approved {a.click_approved}\nAuto-approved {a.auto_approved}\n"
                f"Rejected {a.rejected} · expired {a.expired}"
                + (f"\n{_gate_fact(sc.auto_approve.line)}" if sc.auto_approve else ""),
            ),
            (
                "P&L",
                f"Realised {_usd(p.realised)}\n"
                f"Wins {p.wins} · losses {p.losses}\n"
                f"Open {p.open_positions}"
                + (f" (mark {_usd(p.open_marked_pnl)})" if p.open_marked_pnl is not None else ""),
            ),
            (
                "Order budget",
                f"Peak day {peak}/{lim.daily_max}\n"
                f"Orders {sum(d.orders for d in sc.budget_days)} in {len(sc.budget_days)} day(s)\n"
                f"Tier hits {', '.join(f'{k} {v}' for k, v in tiers.items()) or 'none'}",
            ),
        ]
    )
    blocks: list[Block | None] = [B.header(title), summary, *grid]

    early = [
        f"• {B.esc(c.ticker)} {B.esc(c.exit_reason.replace('_', ' '))}: realised "
        f"{_usd(c.realised_pnl)} vs hold {_usd(c.shadow_hold_pnl)} "
        f"(edge {_usd(c.early_exit_edge)})"
        for c in sc.early_closed
    ]
    known = [c.early_exit_edge for c in sc.early_closed if c.early_exit_edge is not None]
    if early:
        early.append(
            f"Net early-exit edge {_usd(sum(known)) if known else 'pending'} "
            f"({len(known)} of {len(sc.early_closed)} known)"
        )
    blocks.append(_section("Early exits vs hold to expiry (D19)", _more(early)))
    same_day = sc.same_day_closed
    if same_day:
        blocks.append(
            _section(
                "Same-day exits (days held 0)",
                _more(
                    [
                        f"• {B.esc(c.ticker)} {B.esc(c.exit_reason.replace('_', ' '))}: "
                        f"realised {_usd(c.realised_pnl)}"
                        for c in same_day
                    ]
                    + [
                        f"{len(same_day)} of {len(sc.early_closed)} early exit(s) closed the "
                        "day they opened"
                    ]
                ),
            )
        )
    swaps = [
        f"• {B.esc(s.close_ticker)} → {B.esc(s.open_ticker)} ({B.esc(s.status)}): net "
        f"{_usd(s.net)} vs hold {_usd(s.closed_hold_shadow)} (swap edge {_usd(s.vs_hold)})"
        for s in sc.swaps
    ]
    blocks.append(_section("Close-to-reallocate swaps", _more(swaps)))

    m = sc.model_vs_realised
    if m.n:
        blocks.append(
            _section(
                "Modelled vs realised (D23)",
                [
                    f"Managed net EV {_usd(m.managed_net_ev)} · PoP {_pct(m.mean_managed_pop)}",
                    f"Hold to expiry net EV {_usd(m.static_net_ev)} · PoP "
                    f"{_pct(m.mean_static_pop)}",
                    f"Realised {_usd(m.realised)} · win rate {_pct(m.win_rate)} ({m.n} trades)",
                ],
            )
        )
    s = sc.slippage
    if s.fills:
        modelled = (
            f" vs modelled {_usd(s.expected_usd)} (same fills {_usd(s.realised_on_modelled_usd)})"
            if s.expected_usd is not None
            else ""
        )
        blocks.append(
            _section(
                "Slippage", [f"Realised {_usd(s.realised_usd)} on {s.fills} fill(s){modelled}"]
            )
        )
    violations = [f"• {B.esc(k)} ({v})" for k, v in sc.gate_violations.items()]
    blocks.append(_section("Gate violations", _more(violations)))
    calib = [
        f"• {B.esc(b.persona)} [{b.lo:.1f}, {b.hi:.1f}) n={b.n}: stated {b.stated_mean:.0%}, "
        f"realised {b.hit_rate:.0%}"
        for b in sc.calibration
    ]
    blocks.append(
        _section(f"Calibration (all {sc.calibration_trades} closed trades)", _more(calib))
    )
    if report_path:
        blocks.append(B.summary(f"Full report: `{B.esc(report_path)}`"))
    kept = [b for b in blocks if b is not None][: B.MAX_BLOCKS - 1]
    kept.append(B.footer(run=run_id, chain=chain_run_id))
    return CardView(text=B.esc(title[: B.HEADER_MAX]), blocks=kept)
