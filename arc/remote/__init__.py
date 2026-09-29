"""Remote access over Tailscale (E8.6, PLAN D29).

``hermes dashboard`` (web admin, Chat tab, the Hermes Desktop remote backend) on
``<tailscale-ip>:4174``, next to the E8.3 control tower on ``:1994``. Both bind only
the address :func:`arc.tower.net.resolve_bind_address` returns (Tailscale or
loopback); nothing here can widen it.
"""

DASHBOARD_PORT = 4174
TOWER_PORT = 1994

# The three keys `hermes/remote/set-password.sh` writes to ~/.hermes/.env. Hermes'
# bundled `basic` dashboard-auth provider reads them.
BASIC_AUTH_KEYS = (
    "HERMES_DASHBOARD_BASIC_AUTH_USERNAME",
    "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH",
    "HERMES_DASHBOARD_BASIC_AUTH_SECRET",
)
# Plaintext form the provider also accepts; never allowed at rest for E8.6.
PLAINTEXT_PASSWORD_KEY = "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"

__all__ = ["BASIC_AUTH_KEYS", "DASHBOARD_PORT", "PLAINTEXT_PASSWORD_KEY", "TOWER_PORT"]
