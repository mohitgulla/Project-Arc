#!/usr/bin/env bash
# Install (or re-install) the Arc Sentinel skill into its Hermes profile (E9.4 step 7, PLAN D23).
#
#   hermes/sentinel/install.sh [--dry-run]
#
# Copies hermes/sentinel/skills/arc-sentinel/SKILL.md to
#   <profile>/skills/arc-sentinel/SKILL.md
# plus the shared helper skills (D42) from hermes/shared-skills/<name>/ to <profile>/skills/<name>/.
# references/lessons.md is the Sentinel's own calibration log and is never touched.
# The pre-run gate script and the cron are owned by the profile and are not changed here.
# Lens 7 (strategy) is info-only and cross-references the Arc Analyst (hermes/analyst/).
# Lens 8 (experiments integrity, D44/E10.6): sentinel_experiments.py + experiment_areas.json are
# copied into <profile>/scripts/; the agent runs it per its skill (stdlib only, reads the
# gate's RUN_DIR/arc-copy.db and the private clone).
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

# Lens 8 (experiments integrity): the deterministic evidence script and its area map.
for f in sentinel_experiments.py experiment_areas.json; do
  [[ -f "$HERE/$f" ]] || { echo "error: $HERE/$f missing" >&2; exit 1; }
done
run mkdir -p "$PROFILE_HOME/scripts"
run install -m 0644 "$HERE/sentinel_experiments.py" "$PROFILE_HOME/scripts/sentinel_experiments.py"
run install -m 0644 "$HERE/experiment_areas.json" "$PROFILE_HOME/scripts/experiment_areas.json"

# Shared helper skills (PLAN D42): tool how-tos only; the Sentinel skill's rules win.
HELPERS=(defuddle agent-reach code-review-and-quality security-and-hardening performance-optimization)
for h in "${HELPERS[@]}"; do
  hs="$HERE/../shared-skills/$h"
  [[ -f "$hs/SKILL.md" ]] || { echo "error: $hs/SKILL.md missing" >&2; exit 1; }
  run rm -rf "$PROFILE_HOME/skills/$h"
  run cp -R "$hs" "$PROFILE_HOME/skills/$h"
done
echo "installed arc-sentinel skill into $PROFILE_HOME"
