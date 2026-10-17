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
# - Copies the skill, the pre-run gate scripts and SOUL.md from this directory into the profile:
#     <profile>/skills/arc-analyst/SKILL.md, <profile>/scripts/arc_analyst.py,
#     <profile>/scripts/arc_analyst_verdict.py, <profile>/SOUL.md
#   plus the shared helper skills (D42) hermes/shared-skills/{defuddle,agent-reach} to
#   <profile>/skills/<name>/, and the experiment idea seed (E21.2, D86) to
#   <profile>/analyst/idea_seed.yaml
#   (references/lessons.md is created empty once and never overwritten: it is the Analyst's
#   own calibration log).
# - Creates two crons in that profile, both Fable 5.1 at max reasoning, delivery to #arc-analyst:
#   * `arc-analyst-weekly-audit`: Sunday 14:00 PT (after the E7.3 weekly scorecard). It is
#     created PAUSED: E9.4's start condition (>= 1 closed outcome in the journal) gated the
#     first start. E9.4 is done, so after every re-run resume it by hand:
#       hermes -p arc-analyst cron resume <id>
#   * `arc-analyst-verdict-check` (E26.4, D121): daily 09:30 PT, script arc_analyst_verdict.py
#     (= `arc_analyst.py gate --mode verdict`; Hermes runs a cron script without arguments).
#     The gate stays silent unless a new BT holdout verdict or XP verdict file landed, at most
#     once per ET day, and never when the weekly run is due within 24 h. It is created ACTIVE:
#     the re-pause applies to the weekly job only.
# - Idempotent: an existing job with the same name is removed first; re-running re-pauses the
#   weekly job (and re-creates the verdict job active).
# - The gates need no secret: no Alpaca key, no ARC_GATE_SECRET, no Slack token. The weekly gate
#   reads a backup-API copy of ~/GitHub/Project-Arc/data/arc.db (override with ARC_REPO /
#   ARC_ANALYST_DB); the verdict gate reads only the research ledger and the verdict files.
#
# --dry-run: copy nothing, create nothing; print what would be done (used by the tests).
set -euo pipefail

PROFILE="arc-analyst"
NAME="arc-analyst-weekly-audit"
SCHEDULE="0 14 * * 0"          # Sunday 14:00, host local time (PT)
VERDICT_NAME="arc-analyst-verdict-check"
VERDICT_SCHEDULE="30 9 * * *"  # daily 09:30, host local time (PT)
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

for f in arc_analyst.py arc_analyst_verdict.py prompt.md prompt_verdict.md \
         skills/arc-analyst/SKILL.md idea_seed.yaml; do
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
run install -m 0755 "$HERE/arc_analyst_verdict.py" "$PROFILE_HOME/scripts/arc_analyst_verdict.py"
# E21.2 (D86): the experiment idea seed (retired dev drafts as leads). Repo-owned: every
# install overwrites it; the Analyst's own backlog lives elsewhere (E21.3).
run install -m 0644 "$HERE/idea_seed.yaml" "$PROFILE_HOME/analyst/idea_seed.yaml"
if [[ -f "$HERE/SOUL.md" ]]; then
  run install -m 0644 "$HERE/SOUL.md" "$PROFILE_HOME/SOUL.md"
else
  echo "warning: $HERE/SOUL.md not in the repo; the profile keeps its current SOUL.md" >&2
fi
# Shared helper skills (PLAN D42): web-fetch how-tos only; the Analyst skill's caps win.
HELPERS=(defuddle agent-reach)
for h in "${HELPERS[@]}"; do
  hs="$HERE/../shared-skills/$h"
  [[ -f "$hs/SKILL.md" ]] || { echo "error: $hs/SKILL.md missing" >&2; exit 1; }
  run rm -rf "$PROFILE_HOME/skills/$h"
  run cp -R "$hs" "$PROFILE_HOME/skills/$h"
done
lessons="$PROFILE_HOME/skills/arc-analyst/references/lessons.md"
if (( dry )) || [[ ! -f "$lessons" ]]; then
  run touch "$lessons"
fi

if (( ! dry )); then
  for n in "$NAME" "$VERDICT_NAME"; do
    existing="$("$HERMES" -p "$PROFILE" cron list 2>/dev/null | awk -v n="$n" '
      /^  [0-9a-f]+ \[/ {id=$1} $1=="Name:" && $2==n {print id}')"
    for id in $existing; do
      "$HERMES" -p "$PROFILE" cron remove "$id" >/dev/null
      echo "removed previous $n job $id"
    done
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

# E26.4 (D121): created active (no --paused); the gate is the throttle.
run "$HERMES" -p "$PROFILE" cron create "$VERDICT_SCHEDULE" "$(cat "$HERE/prompt_verdict.md")" \
  --name "$VERDICT_NAME" \
  --skill arc-analyst \
  --script arc_analyst_verdict.py \
  --model "$MODEL" \
  --provider "$PROVIDER" \
  --reasoning-effort "$EFFORT" \
  --deliver "$DELIVER"

if (( ! dry )); then
  "$HERMES" -p "$PROFILE" cron list | awk -v a="$NAME" -v b="$VERDICT_NAME" '
    /^  [0-9a-f]+ \[/ {blk=$0; show=0; next}
    $1=="Name:" && ($2==a || $2==b) {print blk; show=1} show {print}'
fi
