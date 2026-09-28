#!/usr/bin/env bash
# Install (or re-install) the ONE Hermes cron job that drives Project Arc routines (D16, E5.3).
#
#   hermes/routines/install.sh [REPO_DIR]
#
# REPO_DIR defaults to the main checkout (~/GitHub/Project-Arc); the tick runs that
# checkout's .venv/bin/arc against its data/arc.db and config/routines.yaml.
#
# - Copies arc_routines_tick.py to ~/.hermes/scripts/ (Hermes only runs scripts from there).
# - Creates `arc-routines-tick`: every 5m, --no-agent (no LLM for the cron itself), cwd = REPO_DIR.
# - Idempotent: an existing job with the same name is removed first.
# - Delivery: the script prints nothing on success, so nothing is posted. A crashed or
#   timed-out tick prints its error and Hermes posts it to #project-arc (ops/dev channel).
#   Routine heartbeats and job failures are posted by Arc to the #arc-investor day thread.
set -euo pipefail

NAME="arc-routines-tick"
SCHEDULE="every 5m"
DELIVER="slack:C0C4KBPN7T5"   # #project-arc
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${1:-$HOME/GitHub/Project-Arc}"
REPO="$(cd "$REPO" && pwd)"
SCRIPTS="${HERMES_HOME:-$HOME/.hermes}/scripts"

[[ -x "$REPO/.venv/bin/arc" ]] || { echo "error: $REPO/.venv/bin/arc missing (run: uv sync)" >&2; exit 1; }
"$REPO/.venv/bin/arc" routines validate --config "$REPO/config/routines.yaml"

mkdir -p "$SCRIPTS"
install -m 0755 "$HERE/arc_routines_tick.py" "$SCRIPTS/arc_routines_tick.py"

existing="$(hermes cron list 2>/dev/null | awk -v n="$NAME" '
  /^  [0-9a-f]+ \[/ {id=$1} $1=="Name:" && $2==n {print id}')"
for id in $existing; do
  hermes cron remove "$id" >/dev/null
  echo "removed previous $NAME job $id"
done

hermes cron create "$SCHEDULE" \
  --name "$NAME" \
  --script arc_routines_tick.py \
  --no-agent \
  --workdir "$REPO" \
  --deliver "$DELIVER"

hermes cron list | awk -v n="$NAME" '/^  [0-9a-f]+ \[/ {blk=$0; show=0; next}
  $1=="Name:" && $2==n {print blk; show=1} show {print}'
