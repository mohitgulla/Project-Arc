"""``arc config``: the D26 control panel CLI (the ``!arc`` Slack plugin shells out here).

- ``arc config show [group|key]``          grouped summary / detail
- ``arc config set <key> <value...> [--reason R]``
- ``arc config diff``                      overrides vs the file/env default
- ``arc config history [key] [--limit N]``
- ``arc config revert <change_id|key>``
- ``arc config confirm <code>`` / ``cancel <code>``  riskier-change confirm step
- ``arc config profile <name>``            shortcut for ``set account_profile``
- ``arc config keys``                      the registry as JSON (validation, docs)

``--actor`` is the Slack user id the plugin passes from the platform event
(``local`` = shell access, the owner). ``--json`` prints ``{text, blocks, result}``
for the plugin; otherwise the plain text. Exit 0 on success/unchanged/pending,
2 on a refusal.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import argparse

    from arc.control.service import ControlService
    from arc.control.store import Source
    from arc.slack.blocks import CardView

__all__ = ["add_config_parser", "run_config"]


def add_config_parser(sub: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    p = sub.add_parser("config", help="D26 control panel: view / change / audit the config")
    csub = p.add_subparsers(dest="config_command", required=True)

    def common(sp: argparse.ArgumentParser) -> argparse.ArgumentParser:
        sp.add_argument("--db", default=None, help="SQLite path (default: data/arc.db)")
        sp.add_argument("--json", action="store_true", help="Print {text, blocks, result}")
        sp.add_argument(
            "--actor",
            default="local",
            help="Slack user id of the requester (the plugin passes it); 'local' = shell owner",
        )
        sp.add_argument(
            "--source", choices=("cli", "slack"), default=None, help="Default: slack if --actor"
        )
        sp.add_argument(
            "--post",
            choices=("none", "project-arc", "arc-investor"),
            default="none",
            help="Also post the card to a Slack channel",
        )
        return sp

    s = common(csub.add_parser("show", help="Grouped summary, or one group/key in detail"))
    s.add_argument("what", nargs="?", default=None)
    st = common(csub.add_parser("set", help="Set a key (riskier changes need confirm)"))
    st.add_argument("key")
    st.add_argument("value", nargs="+")
    st.add_argument("--reason", default=None)
    common(csub.add_parser("diff", help="Overrides vs defaults"))
    h = common(csub.add_parser("history", help="Change log, newest first"))
    h.add_argument("key", nargs="?", default=None)
    h.add_argument("--limit", type=int, default=20)
    r = common(csub.add_parser("revert", help="Undo a change id, or reset a key to default"))
    r.add_argument("ref")
    r.add_argument("--reason", default=None)
    c = common(csub.add_parser("confirm", help="Apply a pending riskier change"))
    c.add_argument("code")
    x = common(csub.add_parser("cancel", help="Drop a pending riskier change"))
    x.add_argument("code")
    pr = common(csub.add_parser("profile", help="Shortcut: set account_profile"))
    pr.add_argument("name")
    pr.add_argument("--reason", default=None)
    common(csub.add_parser("keys", help="The tunable registry as JSON"))


def _source(args: argparse.Namespace) -> Source:
    if args.source == "slack" or (args.source is None and args.actor != "local"):
        return "slack"
    return "cli"


def _optionable() -> Any:
    """Optionable check for universe adds: an Alpaca contracts lookup (paper keys)."""

    def check(symbol: str) -> bool:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetOptionContractsRequest

        from arc.data.alpaca import _get_keys

        key, secret = _get_keys()
        client = TradingClient(api_key=key, secret_key=secret, paper=True)
        resp = client.get_option_contracts(
            GetOptionContractsRequest(underlying_symbols=[symbol], limit=1)
        )
        contracts = getattr(resp, "option_contracts", None) or []
        return len(contracts) > 0

    return check


def _service(args: argparse.Namespace) -> ControlService:
    from arc.config import ArcSettings
    from arc.control.effective import open_store
    from arc.control.service import ControlService

    base = ArcSettings()
    conn = open_store(args.db or base.db_path)
    return ControlService(conn, base=base, optionable=_optionable())


def _post(card: CardView, where: str) -> None:
    if where == "none":
        return
    from arc.slack.client import CHANNEL_ARC_INVESTOR, CHANNEL_PROJECT_ARC, ArcSlackClient

    channel = CHANNEL_PROJECT_ARC if where == "project-arc" else CHANNEL_ARC_INVESTOR
    ArcSlackClient().post_thread_root(channel=channel, text=card.text, blocks=card.blocks)


def _emit(
    args: argparse.Namespace, card: CardView, result: dict[str, Any] | None, code: int
) -> int:
    _post(card, args.post)
    if args.json:
        payload = {"text": card.text, "blocks": card.blocks, "result": result}
        sys.stdout.write(json.dumps(payload, default=str) + "\n")
    else:
        sys.stdout.write(card.text + "\n")
    return code


def _keys_json() -> list[dict[str, Any]]:
    from arc.control.registry import REGISTRY

    return [
        {
            "key": t.key,
            "group": t.group.value,
            "type": t.type.value,
            "target": t.target.value,
            "field": t.field,
            "path": list(t.path),
            "bounds": t.bounds,
            "hard_ceiling": t.hard_ceiling,
            "risk": t.risk.value,
            "env": t.env,
            "description": t.description,
        }
        for t in REGISTRY.values()
    ]


def run_config(args: argparse.Namespace) -> int:
    from arc.control import cards
    from arc.control.registry import TunableError

    cmd = args.config_command
    if cmd == "keys":
        sys.stdout.write(json.dumps(_keys_json(), indent=2) + "\n")
        return 0
    svc = _service(args)
    src = _source(args)
    try:
        if cmd == "show":
            views = svc.show(args.what)
            card = (
                cards.key_detail(views)
                if args.what
                else cards.config_summary(views, version=svc.version())
            )
            return _emit(args, card, {"keys": [v.as_json() for v in views]}, 0)
        if cmd == "diff":
            views = svc.diff()
            card = cards.diff_card(views, version=svc.version())
            return _emit(args, card, {"keys": [v.as_json() for v in views]}, 0)
        if cmd == "history":
            rows = svc.history(args.key, limit=args.limit)
            card = cards.history_card(rows, key=args.key)
            return _emit(args, card, {"changes": [r.model_dump(mode="json") for r in rows]}, 0)
    except TunableError as exc:
        from arc.control.service import Result

        res = Result("refused", key=getattr(args, "what", None) or args.key, message=str(exc))
        return _emit(args, cards.change_card(res), res.as_json(), 2)

    if cmd == "set":
        res = svc.set(
            args.key, " ".join(args.value), actor=args.actor, source=src, reason=args.reason
        )
    elif cmd == "profile":
        res = svc.set(
            "account_profile", args.name, actor=args.actor, source=src, reason=args.reason
        )
    elif cmd == "revert":
        res = svc.revert(args.ref, actor=args.actor, source=src, reason=args.reason)
    elif cmd == "confirm":
        res = svc.confirm(args.code, actor=args.actor, source=src)
    elif cmd == "cancel":
        res = svc.cancel(args.code, actor=args.actor, source=src)
    else:  # pragma: no cover - argparse restricts choices
        raise SystemExit(f"unknown config command {cmd!r}")
    code = 2 if res.outcome in ("refused", "error") else 0
    return _emit(args, cards.change_card(res, actor=args.actor), res.as_json(), code)
