#!/usr/bin/env bash
# Install (or re-install) the Arc health-check LaunchAgent (E8.2).
#
#   hermes/monitoring/install.sh [REPO_DIR]          install + load (every 5 min)
#   hermes/monitoring/install.sh --print [REPO_DIR]  print the plist only
#   hermes/monitoring/install.sh --uninstall
#
# Why launchd and not a Hermes cron: the Hermes gateway hosts the cron ticker, so a
# check that runs inside it cannot report the gateway (or the ticker) being down.
#
# The agent runs `arc health check` from REPO_DIR (default ~/GitHub/Project-Arc):
# tick heartbeat freshness, missed routine windows, stuck runs, `hermes gateway status`
# and `hermes cron status`. New/resolved problems are posted to #project-arc once each.
set -euo pipefail

LABEL="com.projectarc.health-check"
INTERVAL=300
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
SCRIPTS="${HERMES_HOME:-$HOME/.hermes}/scripts"

mode="install"
case "${1:-}" in
  --print) mode="print"; shift ;;
  --uninstall) mode="uninstall"; shift ;;
esac

if [[ "$mode" == "uninstall" ]]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "uninstalled $LABEL"
  exit 0
fi

REPO="${1:-$HOME/GitHub/Project-Arc}"
REPO="$(cd "$REPO" && pwd)"
PY="$(command -v python3)"

plist() {
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$SCRIPTS/arc_health_check.py</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>ARC_REPO</key><string>$REPO</string>
    <key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:$HOME/.local/bin</string>
  </dict>
  <key>StartInterval</key><integer>$INTERVAL</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$REPO/data/logs/health-check.launchd.log</string>
  <key>StandardErrorPath</key><string>$REPO/data/logs/health-check.launchd.log</string>
</dict>
</plist>
EOF
}

if [[ "$mode" == "print" ]]; then
  plist
  exit 0
fi

[[ -x "$REPO/.venv/bin/arc" ]] || { echo "error: $REPO/.venv/bin/arc missing (run: uv sync)" >&2; exit 1; }
"$REPO/.venv/bin/arc" routines validate --config "$REPO/config/routines.yaml"

mkdir -p "$SCRIPTS" "$REPO/data/logs" "$(dirname "$PLIST")"
install -m 0755 "$HERE/arc_health_check.py" "$SCRIPTS/arc_health_check.py"
plist > "$PLIST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $LABEL (every ${INTERVAL}s) -> $PLIST"
launchctl print "gui/$(id -u)/$LABEL" | grep -E "state|last exit" || true
