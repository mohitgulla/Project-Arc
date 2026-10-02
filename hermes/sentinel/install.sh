#!/usr/bin/env bash
# Install (or re-install) the Arc Sentinel skill into its Hermes profile (E9.4 step 7, PLAN D23).
#
#   hermes/sentinel/install.sh [--dry-run]
#
# Copies hermes/sentinel/skills/arc-sentinel/SKILL.md to
#   <profile>/skills/arc-sentinel/SKILL.md
# references/lessons.md is the Sentinel's own calibration log and is never touched.
# The pre-run gate script and the cron are owned by the profile and are not changed here.
# Lens 7 (strategy) is info-only and cross-references the Arc Analyst (hermes/analyst/).
#
# --dry-run: copy nothing; print what would be done (used by the tests).
set -euo pipefail

PROFILE="arc-sentinel"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROFILE_HOME="${ARC_SENTINEL_PROFILE_HOME:-$HOME/.hermes/profiles/$PROFILE}"

dry=0
[[ "${1:-}" == "--dry-run" ]] && dry=1

run() {
  if (( dry )); then echo "+ $*"; else "$@"; fi
}

src="$HERE/skills/arc-sentinel/SKILL.md"
[[ -f "$src" ]] || { echo "error: $src missing" >&2; exit 1; }
if (( ! dry )) && [[ ! -d "$PROFILE_HOME" ]]; then
  echo "error: profile $PROFILE missing ($PROFILE_HOME)" >&2
  exit 1
fi

run mkdir -p "$PROFILE_HOME/skills/arc-sentinel/references"
run install -m 0644 "$src" "$PROFILE_HOME/skills/arc-sentinel/SKILL.md"
echo "installed arc-sentinel skill into $PROFILE_HOME"
