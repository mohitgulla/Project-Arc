"""Slack text + Block Kit for the D26 control panel (shared D22 layout, pure).

Titles follow ``[Control] <What>: <subject> • <fact> • <fact>``, e.g.
``[Control] Set: max_alloc_pct • 5% → 4% • safer``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from arc.control.registry import Group, format_value, lookup
from arc.slack.blocks import CardView, bullets, facts, footer, header, summary

if TYPE_CHECKING:
    from arc.control.service import KeyView, Result
    from arc.control.store import ConfigChange

__all__ = [
    "CONFIRM_ACTION",
    "CANCEL_ACTION",
    "change_card",
    "config_summary",
    "diff_card",
    "history_card",
    "key_detail",
]

CONFIRM_ACTION = "arc_cfg_confirm"
CANCEL_ACTION = "arc_cfg_cancel"
TAG = "[Control]"


def _text_from(title: str, lines: list[str]) -> str:
    return "\n".join([f"*{title}*", *lines])


def change_card(r: Result, *, actor: str | None = None) -> CardView:
    """Card for a set / revert / confirm / cancel / refusal."""
    key = r.key or "config"
    j = r.as_json()
    old_t, new_t = j.get("old_text"), j.get("new_text")
    what = {
        "applied": "Set",
        "reverted": "Revert",
        "pending": "Confirm needed",
        "cancelled": "Cancelled",
        "unchanged": "Unchanged",
        "refused": "Refused",
        "error": "Error",
    }[r.outcome]
    parts = [f"{TAG} {what}: {key}"]
    if old_t is not None and r.outcome not in ("refused", "error"):
        parts.append(f"{old_t} → {new_t}")
    if r.direction and r.outcome != "refused":
        parts.append(r.direction)
    title = " • ".join(parts)
    lines: list[str] = []
    pairs: list[tuple[str, str]] = []
    if r.outcome == "refused":
        lines.append(f"Not changed: {r.message}")
    elif r.outcome == "pending" and r.pending is not None:
        lines.append(
            f"Riskier change. Confirm within the TTL with the button or "
            f"`!arc confirm {r.pending.code}` (cancel: `!arc cancel {r.pending.code}`)."
        )
        pairs += [
            ("Code", f"`{r.pending.code}`"),
            ("Expires", r.pending.expires_at.strftime("%H:%M %Z")),
        ]
    elif r.outcome in ("applied", "reverted"):
        lines.append(f"Applies at the next tick (config version {r.config_version}).")
    else:
        lines.append(r.message)
    if r.halted:
        lines.append("⚠ Trading is halted (`!halt` active); the change is recorded and flagged.")
    if actor:
        pairs.append(("By", f"<@{actor}>" if actor != "local" else "local CLI"))
    blocks: list[dict[str, Any]] = [header(title), summary(*lines)]
    if pairs:
        blocks += facts(pairs)
    if r.outcome == "pending" and r.pending is not None:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": CONFIRM_ACTION,
                        "text": {"type": "plain_text", "text": "Confirm"},
                        "style": "danger",
                        "value": r.pending.code,
                    },
                    {
                        "type": "button",
                        "action_id": CANCEL_ACTION,
                        "text": {"type": "plain_text", "text": "Cancel"},
                        "value": r.pending.code,
                    },
                ],
            }
        )
    blocks.append(
        footer(
            change=str(r.change_id) if r.change_id else None,
            pending=r.pending.id if r.pending else None,
        )
    )
    text_lines = [*lines, *(f"{k}: {v}" for k, v in pairs)]
    return CardView(text=_text_from(title, text_lines), blocks=blocks)


def _line(v: KeyView) -> str:
    mark = " ✎" if v.overridden else ""
    return f"`{v.tunable.key}` = {format_value(v.tunable, v.value)}{mark}"


def config_summary(views: list[KeyView], *, version: int) -> CardView:
    """``!arc config``: every key grouped; ✎ marks an override."""
    title = f"{TAG} Config: {len(views)} keys • version {version}"
    n_over = sum(1 for v in views if v.overridden)
    blocks: list[dict[str, Any]] = [
        header(title),
        summary(f"{n_over} overridden (✎)", "`!arc config <group|key>` for detail"),
    ]
    lines = [f"{n_over} overridden (✎)"]
    for g in Group:
        items = [_line(v) for v in views if v.tunable.group is g]
        if not items:
            continue
        b = bullets(g.value.capitalize(), items, escape=False)
        if b:
            blocks.append(b)
        lines.append(f"\n*{g.value.capitalize()}*")
        lines += [f"• {i}" for i in items]
    lines.append("\n_`!arc config <group|key>` · `!arc set <key> <value> [-- reason]`_")
    return CardView(text=_text_from(title, lines), blocks=blocks[:50])


def key_detail(views: list[KeyView]) -> CardView:
    """``!arc config <group|key>``: value, default, bounds, last change."""
    if len(views) != 1:
        group = views[0].tunable.group.value if views else "-"
        title = f"{TAG} Config: {group} • {len(views)} keys"
        items = [f"{_line(v)} ({v.tunable.bounds})" for v in views] or ["No keys."]
        b = bullets("Keys (bounds)", items, escape=False)
        return CardView(
            text=_text_from(title, [f"• {i}" for i in items]),
            blocks=[header(title), *([b] if b else [])],
        )
    v = views[0]
    t = v.tunable
    title = f"{TAG} Config: {t.key} • {format_value(t, v.value)}"
    last = (
        f"#{v.last.id} {v.last.at:%Y-%m-%d %H:%M} by {v.last.actor}" if v.last else "never changed"
    )
    pairs = [
        ("Value", format_value(t, v.value)),
        ("Default", format_value(t, v.default)),
        ("Bounds", t.bounds),
        ("Hard ceiling", format_value(t, t.hard_ceiling) if t.hard_ceiling is not None else "-"),
        ("Riskier when", t.risk.value),
        ("Last change", last),
    ]
    blocks = [header(title), summary(t.description), *facts(pairs)]
    return CardView(
        text=_text_from(title, [t.description, *(f"{k}: {val}" for k, val in pairs)]),
        blocks=blocks,
    )


def diff_card(views: list[KeyView], *, version: int) -> CardView:
    title = f"{TAG} Diff: {len(views)} overrides • version {version}"
    items = [
        f"`{v.tunable.key}`: {format_value(v.tunable, v.default)} → "
        f"{format_value(v.tunable, v.value)}"
        for v in views
    ]
    if not items:
        items_text = ["Everything is at the file/env default."]
        return CardView(
            text=_text_from(title, items_text), blocks=[header(title), summary(*items_text)]
        )
    b = bullets("Default → effective", items, escape=False)
    return CardView(
        text=_text_from(title, [f"• {i}" for i in items]),
        blocks=[header(title), *([b] if b else [])],
    )


def _fmt(key: str, v: Any, is_default: bool) -> str:
    try:
        t = lookup(key)
    except ValueError:
        return str(v)
    return "default" if is_default else format_value(t, v)


def history_card(rows: list[ConfigChange], *, key: str | None = None) -> CardView:
    title = f"{TAG} History: {key or 'all keys'} • {len(rows)} changes"
    items = []
    for c in rows:
        old = _fmt(c.key, c.old, False)
        new = _fmt(c.key, c.new, c.is_default)
        flag = " ⚠halted" if c.halted else ""
        why = f" — {c.reason}" if c.reason else ""
        sup = f" (undoes #{c.supersedes_id})" if c.supersedes_id else ""
        items.append(
            f"#{c.id} {c.at:%m-%d %H:%M} `{c.key}` {old} → {new} · {c.status}{sup} · "
            f"{c.direction} · {c.actor}{flag}{why}"
        )
    if not items:
        items = ["No changes recorded."]
    b = bullets("Newest first", items, escape=False)
    return CardView(
        text=_text_from(title, [f"• {i}" for i in items]),
        blocks=[header(title), *([b] if b else [])],
    )
