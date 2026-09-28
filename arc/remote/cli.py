"""``arc remote dashboard`` (E8.6): start ``hermes dashboard`` on the Tailscale address.

``hermes/remote/run_dashboard.sh`` (the ``com.projectarc.hermes-dashboard``
LaunchAgent) execs this. Before it replaces itself with ``hermes dashboard`` it
checks, in order, and exits 2 with a one-line reason on the first failure:

1. **Bind address.** :func:`arc.tower.net.resolve_bind_address`: a Tailscale
   ``100.64/10`` IPv4, or loopback with ``--local``. No Tailscale address → refuse.
   There is no flag, env var or config key that widens it (``--address`` accepts
   only what :func:`~arc.tower.net.allowed_bind` accepts).
2. **Basic auth configured.** The three ``HERMES_DASHBOARD_BASIC_AUTH_*`` keys are
   set (process env or ``~/.hermes/.env``) and there is no plaintext
   ``..._PASSWORD`` key. Hermes itself refuses a non-loopback bind without a
   provider; this makes the reason explicit. Only key *names* are ever printed.
3. **No second copy.** ``hermes dashboard --status`` reports nothing running, and
   nothing is listening on the port (loopback or the bind address). The port probe
   matters: ``--status`` matches process argv, and a dashboard started through the
   source launcher (``python3 -I -c …``) does not show up in it.

Then ``exec hermes dashboard --host <ip> --port 9119 --no-open --skip-build``.
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from arc.remote import BASIC_AUTH_KEYS, DASHBOARD_PORT, PLAINTEXT_PASSWORD_KEY

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable, Mapping

log = structlog.get_logger(__name__)

HERMES_ENV = Path.home() / ".hermes" / ".env"


class PreflightError(RuntimeError):
    """A pre-start check failed; the dashboard must not start."""


def add_remote_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("remote", help="Remote access over Tailscale (E8.6)")
    rsub = p.add_subparsers(dest="remote_command", required=True)

    d = rsub.add_parser(
        "dashboard", help="Start `hermes dashboard` on the Tailscale address, :9119 (basic auth)"
    )
    d.add_argument("--port", type=int, default=DASHBOARD_PORT)
    d.add_argument("--address", default=None, help="Tailscale (100.64/10) or loopback IP")
    d.add_argument("--local", action="store_true", help="Bind 127.0.0.1 (this machine only)")
    d.add_argument("--env-file", default=None, help=f"dotenv to check (default {HERMES_ENV})")
    d.add_argument("--hermes-bin", default="hermes", help="hermes CLI (PATH, then ~/.local/bin)")
    d.add_argument("--print-command", action="store_true", help="Run the checks, print the argv")


def _write(text: str) -> None:
    sys.stdout.write(text + "\n")


# ---------------------------------------------------------------------------
# checks (pure where possible, injectable for tests)
# ---------------------------------------------------------------------------


def read_dotenv_keys(path: Path) -> dict[str, bool]:
    """``{KEY: has_non_empty_value}`` for a dotenv file. Values are never returned."""
    out: dict[str, bool] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key.strip()] = bool(value.strip())
    return out


def check_basic_auth(env_file: Path, environ: Mapping[str, str]) -> None:
    """Raise unless the three basic-auth keys are set and no plaintext password is."""
    present = read_dotenv_keys(env_file)
    missing = [k for k in BASIC_AUTH_KEYS if not (environ.get(k, "").strip() or present.get(k))]
    if missing:
        msg = (
            f"dashboard basic auth not configured: missing {', '.join(missing)} "
            f"(run hermes/remote/set-password.sh)"
        )
        raise PreflightError(msg)
    if environ.get(PLAINTEXT_PASSWORD_KEY, "").strip() or present.get(PLAINTEXT_PASSWORD_KEY):
        msg = (
            f"{PLAINTEXT_PASSWORD_KEY} is set: remove the plaintext password "
            f"(set-password.sh stores only the hash)"
        )
        raise PreflightError(msg)


def port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """True when something accepts a TCP connection at host:port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def resolve_hermes(configured: str) -> str | None:
    from arc.monitoring.checks import resolve_hermes as _resolve

    return _resolve(configured)


def hermes_status(hermes: str, timeout: float = 60.0) -> tuple[int, str]:
    """``hermes dashboard --status``: (returncode, output)."""
    from arc.monitoring.checks import run_command

    return run_command([hermes, "dashboard", "--status"], timeout)


def check_not_running(
    hermes: str,
    address: str,
    port: int,
    *,
    status: Callable[[str], tuple[int, str]] = hermes_status,
    probe: Callable[[str, int], bool] = port_open,
) -> None:
    """Raise if a Hermes dashboard/serve is already running, or the port is taken."""
    code, out = status(hermes)
    text = out.strip()
    if code != 0:
        msg = f"`hermes dashboard --status` exited {code}: {text[-200:]}"
        raise PreflightError(msg)
    if not text.startswith("No hermes dashboard"):
        first = text.splitlines()[0] if text else "?"
        msg = f"a Hermes dashboard/serve is already running ({first}); not starting a second copy"
        raise PreflightError(msg)
    for host in dict.fromkeys(("127.0.0.1", address)):
        if probe(host, port):
            msg = f"port {port} already in use on {host}; not starting a second dashboard"
            raise PreflightError(msg)


def dashboard_argv(hermes: str, address: str, port: int) -> list[str]:
    return [
        hermes,
        "dashboard",
        "--host",
        address,
        "--port",
        str(port),
        "--no-open",
        "--skip-build",
    ]


def preflight(
    args: argparse.Namespace,
    *,
    environ: Mapping[str, str] | None = None,
    status: Callable[[str], tuple[int, str]] = hermes_status,
    probe: Callable[[str, int], bool] = port_open,
) -> list[str]:
    """Run every check; return the ``hermes dashboard`` argv. Raises :class:`PreflightError`."""
    from arc.tower.net import NoTailscaleAddressError, resolve_bind_address

    env = os.environ if environ is None else environ
    try:
        address = resolve_bind_address(args.address, local=args.local)
    except NoTailscaleAddressError as exc:
        raise PreflightError(str(exc)) from exc
    env_file = Path(args.env_file).expanduser() if args.env_file else HERMES_ENV
    check_basic_auth(env_file, env)
    hermes = resolve_hermes(args.hermes_bin)
    if hermes is None:
        msg = f"hermes CLI not found ({args.hermes_bin!r})"
        raise PreflightError(msg)
    check_not_running(hermes, address, args.port, status=status, probe=probe)
    return dashboard_argv(hermes, address, args.port)


def _dashboard(args: argparse.Namespace) -> int:
    try:
        argv = preflight(args)
    except PreflightError as exc:
        _write(f"error: {exc}")
        log.warning("remote.dashboard.refused", reason=str(exc))
        return 2
    if args.print_command:
        _write(" ".join(argv))
        return 0
    _write(f"arc remote: hermes dashboard on http://{argv[3]}:{args.port} (basic auth)")
    log.info("remote.dashboard.exec", address=argv[3], port=args.port)
    sys.stdout.flush()
    os.execv(argv[0], argv)  # noqa: S606 - fixed argv, no shell; launchd supervises the pid
    return 0  # pragma: no cover - execv does not return


def run_remote(args: argparse.Namespace) -> int:
    return _dashboard(args)
