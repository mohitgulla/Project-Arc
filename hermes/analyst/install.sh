#!/usr/bin/env bash
# Install (or re-install) the Arc Analyst into its own Hermes profile (E9.2, PLAN D23 sibling).
#
#   hermes/analyst/install.sh [--dry-run]
#
# Prerequisite (owner, E9.4 step 1): the `arc-analyst` profile exists
#   hermes profile create arc-analyst --no-skills
#   then mirror ~/.hermes/profiles/arc-sentinel/config.yaml (model/memory settings). Auth is the
#   Claude subscription route, same as arc-sentinel: the profile .env carries only
#   CLAUDE_CODE_OAUTH_TOKEN (no ANTHROPIC_API_KEY, no Alpaca keys, no ARC_GATE_SECRET).
#   Verify with: hermes -p arc-analyst auth status anthropic
#
# - Copies the skill, the pre-run gate script and SOUL.md from this directory into the profile:
#     <profile>/skills/arc-analyst/SKILL.md, <profile>/scripts/arc_analyst.py, <profile>/SOUL.md
#   (references/lessons.md is created empty once and never overwritten: it is the Analyst's
#   own calibration log).
# - Creates the cron `arc-analyst-weekly-audit` in that profile: Sunday 14:00 PT (after the E7.3 weekly
#   scorecard), Fable 5.1 at max reasoning, delivery to #arc-analyst. It is created PAUSED:
#   E9.4 decides when it starts (>= 1 closed outcome in the journal).
# - Idempotent: an existing job with the same name is removed first; re-running re-pauses it.
# - The gate needs no secret: no Alpaca key, no ARC_GATE_SECRET, no Slack token. It reads a
#   backup-API copy of ~/GitHub/Project-Arc/data/arc.db (override with ARC_REPO / ARC_ANALYST_DB).
#
# --dry-run: copy nothing, create nothing; print what would be done (used by the tests).
set -euo pipefail

PROFILE="arc-analyst"
NAME="arc-analyst-weekly-audit"
SCHEDULE="0 14 * * 0"          # Sunday 14:00, host local time (PT)
MODEL="anthropic/claude-fable-5.1"
PROVIDER="anthropic"
EFFORT="max"
DELIVER="slack:C0C5CB72LG5"    # #arc-analyst (E9.1)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROFILE_HOME="${ARC_ANALYST_PROFILE_HOME:-$HOME/.hermes/profiles/$PROFILE}"
HERMES="${HERMES_BIN:-hermes}"

dry=0
[[ "${1:-}" == "--dry-run" ]] && dry=1

run() {
  if (( dry )); then printf '+'; printf ' %q' "$@"; printf '\n'; else "$@"; fi
}

for f in arc_analyst.py prompt.md skills/arc-analyst/SKILL.md; do
  [[ -f "$HERE/$f" ]] || { echo "error: $HERE/$f missing" >&2; exit 1; }
done
if (( ! dry )) && [[ ! -d "$PROFILE_HOME" ]]; then
  echo "error: profile $PROFILE missing ($PROFILE_HOME); run E9.4 step 1 first:" >&2
  echo "  hermes profile create $PROFILE --no-skills" >&2
  exit 1
fi

run mkdir -p "$PROFILE_HOME/skills/arc-analyst/references" "$PROFILE_HOME/scripts" \
  "$PROFILE_HOME/analyst"
run install -m 0644 "$HERE/skills/arc-analyst/SKILL.md" "$PROFILE_HOME/skills/arc-analyst/SKILL.md"
run install -m 0755 "$HERE/arc_analyst.py" "$PROFILE_HOME/scripts/arc_analyst.py"
if [[ -f "$HERE/SOUL.md" ]]; then
  run install -m 0644 "$HERE/SOUL.md" "$PROFILE_HOME/SOUL.md"
else
  echo "warning: $HERE/SOUL.md not in the repo; the profile keeps its current SOUL.md" >&2
fi
lessons="$PROFILE_HOME/skills/arc-analyst/references/lessons.md"
if (( dry )) || [[ ! -f "$lessons" ]]; then
  run touch "$lessons"
fi

if (( ! dry )); then
  existing="$("$HERMES" -p "$PROFILE" cron list 2>/dev/null | awk -v n="$NAME" '
    /^  [0-9a-f]+ \[/ {id=$1} $1=="Name:" && $2==n {print id}')"
  for id in $existing; do
    "$HERMES" -p "$PROFILE" cron remove "$id" >/dev/null
    echo "removed previous $NAME job $id"
  done
fi

run "$HERMES" -p "$PROFILE" cron create "$SCHEDULE" "$(cat "$HERE/prompt.md")" \
  --name "$NAME" \
  --skill arc-analyst \
  --script arc_analyst.py \
  --model "$MODEL" \
  --provider "$PROVIDER" \
  --reasoning-effort "$EFFORT" \
  --deliver "$DELIVER" \
  --paused \
  --paused-reason "E9.4: start once outcomes has >= 1 closed row; dry run with delivery local first"

if (( ! dry )); then
  "$HERMES" -p "$PROFILE" cron list | awk -v n="$NAME" '/^  [0-9a-f]+ \[/ {blk=$0; show=0; next}
    $1=="Name:" && $2==n {print blk; show=1} show {print}'
fi
