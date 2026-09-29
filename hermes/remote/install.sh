#!/usr/bin/env bash
# Install (or re-install) the two remote-access LaunchAgents (E8.6 / D29).
#
#   hermes/remote/install.sh [REPO_DIR]              install + load both
#   hermes/remote/install.sh --print [REPO_DIR]      print both plists only
#   hermes/remote/install.sh --uninstall             unload + remove both
#
#   com.projectarc.hermes-dashboard  hermes/remote/run_dashboard.sh
#                                    -> hermes dashboard on <tailscale-ip>:4174 (basic auth)
#   com.projectarc.tower             .venv/bin/arc tower serve
#                                    -> read-only control tower on <tailscale-ip>:1994
#
# Both are KeepAlive + RunAtLoad and bind only the Tailscale address
# (arc.tower.net.resolve_bind_address). Without one they exit 2 and launchd retries
# every ThrottleInterval (60s). Logs: REPO/data/logs/{hermes-dashboard,tower}.launchd.log.
#
# This script only ever boots out its OWN two labels. It never touches
# ai.hermes.gateway (the Slack gateway) or com.projectarc.health-check.
set -euo pipefail

DASH_LABEL="com.projectarc.hermes-dashboard"
TOWER_LABEL="com.projectarc.tower"
LABELS=("$DASH_LABEL" "$TOWER_LABEL")
THROTTLE=60
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"

mode="install"
case "${1:-}" in
  --print) mode="print"; shift ;;
  --uninstall) mode="uninstall"; shift ;;
esac

if [[ "$mode" == "uninstall" ]]; then
  for label in "${LABELS[@]}"; do
    launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    rm -f "$AGENTS/$label.plist"
    echo "uninstalled $label"
  done
  exit 0
fi

REPO="${1:-$HOME/GitHub/Project-Arc}"
REPO="$(cd "$REPO" && pwd)"
LOGS="$REPO/data/logs"
# Same PATH as the health-check agent: `hermes` lives in ~/.local/bin, ifconfig in /sbin.
AGENT_PATH="/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:$HOME/.local/bin"

# plist LABEL LOGNAME PROGRAM_ARGS...
plist() {
  local label="$1" logname="$2"
  shift 2
  local args=""
  for a in "$@"; do args+="    <string>$a</string>"$'\n'; done
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
${args}  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>ARC_REPO</key><string>$REPO</string>
    <key>ARC_ENV</key><string>paper</string>
    <key>PATH</key><string>$AGENT_PATH</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>$THROTTLE</integer>
  <key>StandardOutPath</key><string>$LOGS/$logname.launchd.log</string>
  <key>StandardErrorPath</key><string>$LOGS/$logname.launchd.log</string>
</dict>
</plist>
EOF
}

dash_plist() { plist "$DASH_LABEL" hermes-dashboard /bin/bash "$HERE/run_dashboard.sh"; }
tower_plist() { plist "$TOWER_LABEL" tower "$REPO/.venv/bin/arc" tower serve; }

if [[ "$mode" == "print" ]]; then
  dash_plist
  tower_plist
  exit 0
fi

[[ -x "$REPO/.venv/bin/arc" ]] || { echo "error: $REPO/.venv/bin/arc missing (run: uv sync)" >&2; exit 1; }
mkdir -p "$LOGS" "$AGENTS"
chmod 0755 "$HERE/run_dashboard.sh" "$HERE/set-password.sh"
dash_plist > "$AGENTS/$DASH_LABEL.plist"
tower_plist > "$AGENTS/$TOWER_LABEL.plist"
for label in "${LABELS[@]}"; do
  plutil -lint "$AGENTS/$label.plist" >/dev/null
  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$AGENTS/$label.plist"
  echo "installed $label -> $AGENTS/$label.plist"
done
sleep 3
for label in "${LABELS[@]}"; do
  echo "--- $label"
  launchctl print "$DOMAIN/$label" | grep -E "^\s*(state|last exit code|pid) =" || true
done
echo "logs: $LOGS/hermes-dashboard.launchd.log, $LOGS/tower.launchd.log"
