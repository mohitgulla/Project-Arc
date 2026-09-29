"""/arc — Project Arc status digest + D26 control panel.

Board status (from the `project-arc` kanban board):
       /arc            summary (running, blocked, review PRs, up next, recently done)
       /arc all        also list every todo card
       /arc <task_id>  details for one card (summary, latest comment, thread link)

Control panel (E8.5, D26), shelling out to the project's `arc config` CLI:
       /arc config [group|key]            grouped summary / detail
       /arc set <key> <value> [-- reason] change a key (riskier -> confirm step)
       /arc diff                          overrides vs defaults
       /arc history [key]                 change log, newest first
       /arc revert <change_id|key>        undo a change / reset a key
       /arc profile <name>                shortcut for `set account_profile`
       /arc confirm <code> | cancel <code>
In Slack type `@hermes !arc ...`.

The CLI owns every rule (owner-only, bounds, hard ceilings, confirm, audit). The
requester's Slack id comes from the gateway session (``HERMES_SESSION_USER_ID``),
never from message text; a change without a Slack identity is refused here.
Riskier changes post a card with Confirm / Cancel buttons (``arc_cfg_confirm`` /
``arc_cfg_cancel``, value = the one-time code); the click runs
``arc config confirm <code> --actor <clicker>`` and rewrites the card.

Install (source of truth is the repo): ``cp -r hermes/plugins/arc-status
~/.hermes/plugins/`` (already enabled; slash commands reload live, the button
handlers need one gateway restart). Override the repo with ``ARC_REPO_DIR``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BOARD = "project-arc"
REPO_DIR = Path(os.environ.get("ARC_REPO_DIR", str(Path.home() / "GitHub" / "Project-Arc")))
SLACK_WORKSPACE_URL = "https://arc-4174.slack.com"
MAX_REASON = 220
CONFIG_TIMEOUT_S = 60
CONFIG_COMMANDS = {"config", "set", "diff", "history", "revert", "profile", "confirm", "cancel"}
WRITE_COMMANDS = {"set", "revert", "profile", "confirm", "cancel"}
CONFIRM_ACTIONS = {"arc_cfg_confirm": "confirm", "arc_cfg_cancel": "cancel"}


def _int(v) -> int | None:
    try:
        return int(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _ago(ts, now: int) -> str:
    ts = _int(ts)
    if not ts:
        return "?"
    s = max(0, now - int(ts))
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m ago"
    return f"{s // 86400}d ago"


def _short(text: str | None, n: int = MAX_REASON) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def _open_prs() -> list[dict]:
    try:
        out = subprocess.run(
            ["gh", "pr", "list", "--state", "open", "--json",
             "number,title,headRefName,url,reviewDecision,isDraft"],
            cwd=REPO_DIR, capture_output=True, text=True, timeout=15)
        return json.loads(out.stdout) if out.returncode == 0 and out.stdout.strip() else []
    except Exception as exc:  # gh missing / offline: digest still renders from the board
        logger.warning("arc-status: gh pr list failed: %s", exc)
        return []


def _load(conn) -> dict:
    tasks = [dict(r) for r in conn.execute(
        "SELECT id, title, status, priority, started_at, completed_at, created_at "
        "FROM tasks WHERE status != 'archived'")]
    for t in tasks:  # some rows store epochs as TEXT
        for k in ("priority", "started_at", "completed_at", "created_at"):
            t[k] = _int(t[k])
    ids = [t["id"] for t in tasks]
    ph = ",".join("?" for _ in ids) or "''"
    parents: dict[str, list[str]] = {}
    for r in conn.execute("SELECT parent_id, child_id FROM task_links"):
        parents.setdefault(r["child_id"], []).append(r["parent_id"])
    threads = {r["task_id"]: r["thread_id"] for r in conn.execute(
        "SELECT task_id, chat_id, thread_id FROM kanban_notify_subs WHERE platform='slack'")}
    chats = {r["task_id"]: r["chat_id"] for r in conn.execute(
        "SELECT task_id, chat_id FROM kanban_notify_subs WHERE platform='slack'")}
    runs: dict[str, dict] = {}
    for r in conn.execute(
            f"SELECT * FROM task_runs WHERE task_id IN ({ph}) ORDER BY started_at, id", ids):
        runs[r["task_id"]] = dict(r)  # last wins = latest run
    summaries: dict[str, str] = {}
    for r in conn.execute(
            f"SELECT task_id, summary FROM task_runs WHERE task_id IN ({ph}) "
            "AND summary IS NOT NULL AND summary != '' "
            "ORDER BY COALESCE(ended_at, started_at), id", ids):
        summaries[r["task_id"]] = r["summary"]
    comments: dict[str, str] = {}
    for r in conn.execute(
            f"SELECT task_id, body FROM task_comments WHERE task_id IN ({ph}) ORDER BY id", ids):
        comments[r["task_id"]] = r["body"]
    return dict(tasks=tasks, parents=parents, threads=threads, chats=chats, runs=runs,
                summaries=summaries, comments=comments)


def _label(t: dict, d: dict) -> str:
    title = t["title"]
    ts, chat = d["threads"].get(t["id"]), d["chats"].get(t["id"])
    if ts and chat:
        url = f"{SLACK_WORKSPACE_URL}/archives/{chat}/p{str(ts).replace('.', '')}"
        return f"[{title}]({url})"
    return title


def _pr_for(task_id: str, prs: list[dict]) -> dict | None:
    return next((p for p in prs if task_id in (p.get("headRefName") or "")), None)


def _pr_tag(pr: dict | None) -> str:
    return f" · [PR #{pr['number']}]({pr['url']})" if pr else ""


def _digest(show_all: bool) -> str:
    from hermes_cli.kanban_db_connect import connect_closing

    with connect_closing(board=BOARD) as conn:
        d = _load(conn)
    prs = _open_prs()
    now = int(time.time())
    by_id = {t["id"]: t for t in d["tasks"]}
    by_status: dict[str, list[dict]] = {}
    for t in d["tasks"]:
        by_status.setdefault(t["status"], []).append(t)
    for lst in by_status.values():
        lst.sort(key=lambda t: t["title"])
    total = len(d["tasks"])
    done = len(by_status.get("done", []))
    pct = round(100 * done / total) if total else 0
    bar = "█" * (pct // 10) + "░" * (10 - pct // 10)

    counts = " · ".join(f"{len(by_status.get(s, []))} {s}" for s in
                        ("running", "review", "blocked", "ready", "todo") if by_status.get(s))
    lines = [f"*Project Arc — board `{BOARD}`*",
             f"{bar} {done}/{total} done ({pct}%) · {counts}"]

    running = by_status.get("running", [])
    if running:
        lines.append("\n*🟢 Running*")
        for t in running:
            run = d["runs"].get(t["id"]) or {}
            hb = run.get("last_heartbeat_at")
            hb_txt = f", heartbeat {_ago(hb, now)}" if hb else ""
            lines.append(f"• `{t['id']}` {_label(t, d)} — started {_ago(run.get('started_at') or t['started_at'], now)}{hb_txt}"
                         f"{_pr_tag(_pr_for(t['id'], prs))}")

    review = by_status.get("review", [])
    card_prs = [(p, next((t for t in d["tasks"] if t["id"] in (p.get("headRefName") or "")), None))
                for p in prs]
    if review or card_prs:
        lines.append("\n*👀 Awaiting your review*")
        seen = set()
        for p, t in card_prs:
            name = _label(t, d) if t else p["title"]
            state = (t or {}).get("status", "no card")
            lines.append(f"• [PR #{p['number']}]({p['url']}) {name} — card {state}")
            if t:
                seen.add(t["id"])
        for t in review:
            if t["id"] not in seen:
                lines.append(f"• `{t['id']}` {_label(t, d)} — review requested")

    blocked = by_status.get("blocked", [])
    if blocked:
        lines.append("\n*⛔ Blocked*")
        for t in blocked:
            reason = d["summaries"].get(t["id"]) or d["comments"].get(t["id"])
            waiting = [by_id[p]["title"].split(" · ")[0] for p in d["parents"].get(t["id"], [])
                       if p in by_id and by_id[p]["status"] != "done"]
            why = _short(reason) if reason else (f"waiting on {', '.join(waiting)}" if waiting else "no reason recorded")
            lines.append(f"• `{t['id']}` {_label(t, d)}\n    ↳ {why}")

    # Up next: not started, every parent done.
    pending = by_status.get("ready", []) + by_status.get("todo", [])
    unblocked = [t for t in pending
                 if all(by_id.get(p, {}).get("status") == "done" for p in d["parents"].get(t["id"], []))]
    if unblocked:
        unblocked.sort(key=lambda t: (-(t["priority"] or 0), t["title"]))
        lines.append("\n*⏭ Up next (dependencies met)*")
        for t in unblocked[:6]:
            lines.append(f"• `{t['id']}` {_label(t, d)}")
        if len(unblocked) > 6:
            lines.append(f"  …and {len(unblocked) - 6} more")

    recent = sorted(by_status.get("done", []), key=lambda t: -(t["completed_at"] or 0))[:5]
    if recent:
        lines.append("\n*✅ Recently done*")
        for t in recent:
            lines.append(f"• {_label(t, d)} — {_ago(t['completed_at'], now)}")

    if show_all:
        waiting = [t for t in pending if t not in unblocked]
        if waiting:
            lines.append("\n*◻ Waiting on dependencies*")
            for t in waiting:
                deps = [by_id[p]["title"].split(" · ")[0] for p in d["parents"].get(t["id"], [])
                        if p in by_id and by_id[p]["status"] != "done"]
                lines.append(f"• `{t['id']}` {t['title']} — needs {', '.join(deps)}")
    else:
        lines.append("\n_`!arc all` for the full todo list · `!arc <task_id>` for card detail_")
    return "\n".join(lines)


def _card(task_id: str) -> str:
    from hermes_cli.kanban_db_connect import connect_closing

    with connect_closing(board=BOARD) as conn:
        d = _load(conn)
    t = next((x for x in d["tasks"] if x["id"] == task_id or x["title"].lower().startswith(task_id.lower() + " ")), None)
    if not t:
        return f"No card `{task_id}` on board `{BOARD}`."
    now = int(time.time())
    run = d["runs"].get(t["id"]) or {}
    pr = _pr_for(t["id"], _open_prs())
    by_id = {x["id"]: x for x in d["tasks"]}
    deps = [f"{by_id[p]['title'].split(' · ')[0]} ({by_id[p]['status']})"
            for p in d["parents"].get(t["id"], []) if p in by_id]
    lines = [f"*{_label(t, d)}*", f"`{t['id']}` · status *{t['status']}*{_pr_tag(pr)}"]
    if deps:
        lines.append(f"Depends on: {', '.join(deps)}")
    if run:
        lines.append(f"Last run: {run.get('status')}/{run.get('outcome') or '—'}, started {_ago(run.get('started_at'), now)}")
        if run.get("error"):
            lines.append(f"Error: {_short(run['error'])}")
    if d["summaries"].get(t["id"]):
        lines.append(f"\n*Latest summary*\n{_short(d['summaries'][t['id']], 900)}")
    if d["comments"].get(t["id"]):
        lines.append(f"\n*Latest comment*\n{_short(d['comments'][t['id']], 900)}")
    return "\n".join(lines)


def _render(raw_args: str) -> str:
    arg = (raw_args or "").strip()
    if arg in ("", "all"):
        return _digest(show_all=arg == "all")
    return _card(arg.split()[0])


# ---------------------------------------------------------------------------
# D26 control panel: `!arc config|set|diff|history|revert|profile|confirm|cancel`
# ---------------------------------------------------------------------------


class _Slack:
    """The AsyncApp web client, captured by the ``register_platform_handler`` factory."""

    client: Any = None


SLACK = _Slack()


def _session(name: str) -> str:
    try:
        from gateway.session_context import get_session_env
    except ImportError:  # outside the gateway (tests, CLI)
        return os.environ.get(name, "")
    return get_session_env(name, "")


def _arc_bin() -> Path:
    return REPO_DIR / ".venv" / "bin" / "arc"


# The gateway runs on Hermes's own Python and exports PYTHONPATH/PYTHONHOME for it.
# Inherited by `arc`, they put Hermes's site-packages ahead of Arc's venv and the CLI
# dies importing a foreign pydantic_core ("arc config exit 1"). Arc gets a clean env.
_PY_ENV_PREFIXES = ("PYTHON", "VIRTUAL_ENV", "CONDA_", "UV_", "PIP_", "__PYVENV")


def _arc_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """``os.environ`` minus Python/venv variables, with Arc's venv first on PATH."""
    env = {k: v for k, v in (os.environ if base is None else base).items()
           if not k.startswith(_PY_ENV_PREFIXES)}
    venv_bin = str(REPO_DIR / ".venv" / "bin")
    env["PATH"] = os.pathsep.join([venv_bin, env.get("PATH", "")]).rstrip(os.pathsep)
    return env


def parse_config(raw_args: str) -> list[str] | None:
    """``arc config ...`` argv for a control-panel command, else None (board status).

    ``set <key> <value...> [-- reason]``; ``profile <name> [-- reason]``;
    ``config [group|key]``; ``history [key]``; ``revert <ref> [-- reason]``;
    ``confirm|cancel <code>``; ``diff``.
    """
    text = (raw_args or "").strip()
    reason = ""
    if " -- " in f" {text} ":
        text, _, reason = f" {text} ".partition(" -- ")
        text, reason = text.strip(), reason.strip()
    try:
        words = shlex.split(text)
    except ValueError:
        words = text.split()
    if not words or words[0].lower() not in CONFIG_COMMANDS:
        return None
    cmd, rest = words[0].lower(), words[1:]
    if cmd == "config":
        argv = ["show", *rest[:1]]
    elif cmd == "set":
        if len(rest) < 2:
            return ["usage", "set <key> <value> [-- reason]"]
        argv = ["set", rest[0], *rest[1:]]
    elif cmd in ("profile", "revert", "confirm", "cancel"):
        if len(rest) != 1:
            arg = {"profile": "name", "revert": "change_id|key"}.get(cmd, "code")
            return ["usage", f"{cmd} <{arg}>"]
        argv = [cmd, rest[0]]
    elif cmd == "history":
        argv = ["history", *rest[:1]]
    else:
        argv = ["diff"]
    if reason and cmd in ("set", "profile", "revert"):
        argv += ["--reason", reason[:MAX_REASON]]
    return argv


def run_config(argv: list[str], actor: str) -> dict:
    """Run ``arc config <argv> --json --actor <actor>``; its JSON (or an error dict)."""
    cmd = [str(_arc_bin()), "config", *argv, "--json", "--actor", actor, "--source", "slack"]
    try:
        out = subprocess.run(
            cmd, cwd=REPO_DIR, env=_arc_env(), capture_output=True, text=True,
            timeout=CONFIG_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error("arc-status: arc config failed: %s", exc)
        return {"text": f"⚠️ arc config failed: {exc}", "blocks": None, "result": None}
    try:
        payload = json.loads(out.stdout)
    except json.JSONDecodeError:
        logger.error("arc-status: arc config exit %s: %s", out.returncode, out.stderr[-500:])
        return {"text": f"⚠️ arc config exit {out.returncode}", "blocks": None, "result": None}
    outcome = (payload.get("result") or {}).get("outcome", "ok")
    logger.info("arc-status: config %s by %s -> %s", argv[:2], actor, outcome)
    return payload


async def _post_blocks(payload: dict) -> bool:
    """Post a pending-change card with Confirm/Cancel in the requester's thread (best effort)."""
    if SLACK.client is None or not payload.get("blocks"):
        return False
    chat = _session("HERMES_SESSION_CHAT_ID")
    thread = _session("HERMES_SESSION_THREAD_ID") or None
    if not chat:
        return False
    try:
        await SLACK.client.chat_postMessage(
            channel=chat, thread_ts=thread, text=payload.get("text", ""), blocks=payload["blocks"]
        )
    except Exception as exc:  # noqa: BLE001 - the text reply still carries the code
        logger.warning("arc-status: chat.postMessage failed: %s", exc)
        return False
    return True


async def _handle_config(argv: list[str]) -> str:
    if argv[0] == "usage":
        return f"Usage: `!arc {argv[1]}`"
    platform = _session("HERMES_SESSION_PLATFORM")
    actor = _session("HERMES_SESSION_USER_ID")
    if not actor or (platform and platform != "slack"):
        if argv[0] in WRITE_COMMANDS:
            return "⚠️ Config changes are Slack-only (owner id from the event); use `arc config` locally."
        actor = actor or "anonymous"
    payload = await asyncio.to_thread(run_config, argv, actor)
    result = payload.get("result") or {}
    if result.get("outcome") == "pending" and await _post_blocks(payload):
        return "Confirm-needed card posted above ⤴ (buttons, or type the code)."
    return payload.get("text") or "⚠️ arc config returned nothing"


def parse_click(body: dict, action: dict) -> tuple[str, str, str, str, str] | None:
    """``(command, code, user_id, channel, message_ts)`` for a confirm/cancel click."""
    action_id = str((action or {}).get("action_id") or "")
    if action_id not in CONFIRM_ACTIONS:
        return None
    code = str((action or {}).get("value") or "").strip()
    user = str(((body or {}).get("user") or {}).get("id") or "")
    if not code or not user:
        return None
    container = (body or {}).get("container") or {}
    channel = str(container.get("channel_id") or ((body or {}).get("channel") or {}).get("id") or "")
    return CONFIRM_ACTIONS[action_id], code, user, channel, str(container.get("message_ts") or "")


async def on_confirm_click(ack, body, action) -> None:
    await ack()
    click = parse_click(body, action)
    if click is None:
        logger.warning("arc-status: ignored malformed config click")
        return
    cmd, code, user, channel, ts = click
    payload = await asyncio.to_thread(run_config, [cmd, code], user)
    outcome = (payload.get("result") or {}).get("outcome")
    if SLACK.client is None or not channel or not ts:
        return
    try:
        if outcome == "refused":  # e.g. a non-owner click: leave the buttons for the owner
            await SLACK.client.chat_postMessage(
                channel=channel, thread_ts=ts, text=payload.get("text", "")
            )
        else:
            await SLACK.client.chat_update(
                channel=channel, ts=ts, text=payload.get("text", ""), blocks=payload.get("blocks")
            )
    except Exception as exc:  # noqa: BLE001 - the change itself is already recorded
        logger.warning("arc-status: card update failed: %s", exc)


def slack_handlers(app: Any, adapter: Any = None) -> None:
    """``register_platform_handler("slack", ...)`` factory: keep the web client."""
    SLACK.client = getattr(app, "client", None)


async def _handle(raw_args: str) -> str:
    try:
        argv = parse_config(raw_args)
        if argv is not None:
            return await _handle_config(argv)
        return await asyncio.to_thread(_render, raw_args)
    except Exception as exc:
        logger.exception("arc-status failed")
        return f"⚠️ /arc failed: {exc}"


def register(ctx) -> None:
    ctx.register_command(
        "arc", handler=_handle,
        args_hint="[all|<task_id>|config|set|diff|history|revert|profile|confirm|cancel]",
        description="Project Arc board status + control panel (config/set/revert/confirm)")
    register_action = getattr(ctx, "register_slack_action_handler", None)
    if register_action is not None:
        for action_id in CONFIRM_ACTIONS:
            register_action(action_id, on_confirm_click)
    register_platform = getattr(ctx, "register_platform_handler", None)
    if register_platform is not None:
        register_platform("slack", slack_handlers)
