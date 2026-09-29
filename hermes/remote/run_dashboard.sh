#!/usr/bin/env bash
# LaunchAgent entry point for com.projectarc.hermes-dashboard (E8.6 / D29).
#
#   hermes/remote/run_dashboard.sh [extra `arc remote dashboard` args]
#
# Runs `arc remote dashboard` from the repo venv, which:
#   1. resolves the bind address with arc.tower.net.resolve_bind_address
#      (Tailscale 100.64/10 IPv4 only; never 0.0.0.0 or the LAN),
#   2. checks the three HERMES_DASHBOARD_BASIC_AUTH_* keys are set (no plaintext password),
#   3. checks `hermes dashboard --status` and the port: no second copy,
# then execs `hermes dashboard --host <ts-ip> --port 1994 --no-open --skip-build`.
# Any failed check exits 2 with a one-line reason; launchd retries after ThrottleInterval.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${ARC_REPO:-$(cd "$HERE/../.." && pwd)}"
ARC="$REPO/.venv/bin/arc"

[[ -x "$ARC" ]] || { echo "error: $ARC missing (run: uv sync in $REPO)" >&2; exit 1; }
echo "[$(date '+%Y-%m-%d %H:%M:%S %Z')] run_dashboard: starting"
exec "$ARC" remote dashboard "$@"
