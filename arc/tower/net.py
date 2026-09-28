"""Where the control tower listens: the Tailscale interface, never a public one (E8.3).

:func:`resolve_bind_address` picks, in order:

1. an explicit ``--address`` (must be a Tailscale CGNAT address ``100.64.0.0/10``
   or loopback; anything else is refused, so a typo can't expose the tower on
   the LAN or ``0.0.0.0``);
2. ``--local`` → ``127.0.0.1`` (development on this machine only);
3. ``tailscale ip -4`` (CLI on PATH, or the macOS app bundle);
4. the first ``100.64.0.0/10`` IPv4 address on any interface (``ifconfig`` /
   ``ip -4 addr``).

If none is found it raises :class:`NoTailscaleAddressError`: the tower does not
start rather than falling back to a wider bind.
"""

from __future__ import annotations

import ipaddress
import re
import shutil
import subprocess
from pathlib import Path

import structlog

__all__ = [
    "TAILSCALE_NET",
    "NoTailscaleAddressError",
    "allowed_bind",
    "cgnat_addresses",
    "resolve_bind_address",
]

log = structlog.get_logger(__name__)

TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")
_MAC_APP_CLI = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
_INET = re.compile(r"\binet (?:addr:)?(\d{1,3}(?:\.\d{1,3}){3})")


class NoTailscaleAddressError(RuntimeError):
    """No Tailscale IPv4 address on this host: refuse to start."""


def allowed_bind(address: str) -> bool:
    """True for a Tailscale CGNAT IPv4 address or loopback; False for anything else."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.is_loopback or (ip.version == 4 and ip in TAILSCALE_NET)


def cgnat_addresses(text: str) -> list[str]:
    """Tailscale-range IPv4 addresses in ``tailscale ip`` / ``ifconfig`` / ``ip addr`` output."""
    found: list[str] = []
    for token in [*_INET.findall(text), *text.split()]:
        try:
            ip = ipaddress.ip_address(token.strip())
        except ValueError:
            continue
        if ip.version == 4 and ip in TAILSCALE_NET and str(ip) not in found:
            found.append(str(ip))
    return found


def _run(argv: list[str]) -> str:
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout if out.returncode == 0 else ""


def _tailscale_cli() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    return str(_MAC_APP_CLI) if _MAC_APP_CLI.is_file() else None


def resolve_bind_address(address: str | None = None, *, local: bool = False) -> str:
    """The address the tower binds to (see module doc). Raises if there is no safe one."""
    if address:
        if not allowed_bind(address):
            msg = (
                f"refusing to bind {address!r}: the control tower only listens on a "
                f"Tailscale address ({TAILSCALE_NET}) or loopback"
            )
            raise NoTailscaleAddressError(msg)
        return address
    if local:
        return "127.0.0.1"
    cli = _tailscale_cli()
    if cli:
        ips = cgnat_addresses(_run([cli, "ip", "-4"]))
        if ips:
            log.info("tower.bind", source="tailscale-cli", address=ips[0])
            return ips[0]
    for argv in (["ifconfig"], ["ip", "-4", "addr"]):
        if shutil.which(argv[0]):
            ips = cgnat_addresses(_run(argv))
            if ips:
                log.info("tower.bind", source=argv[0], address=ips[0])
                return ips[0]
    msg = (
        "no Tailscale IPv4 address found (is Tailscale installed and up? "
        "`tailscale ip -4`). Use --local for 127.0.0.1 on this machine only."
    )
    raise NoTailscaleAddressError(msg)
