#!/usr/bin/env bash
# Set the Hermes dashboard login (E8.6 / D29): username + password for the
# bundled `basic` dashboard-auth provider.
#
#   hermes/remote/set-password.sh [REPO_DIR]
#
# - Prompts for the username (default: $USER) and the password twice with
#   `read -s`: the password is never echoed, never in argv, never in a file.
# - Hashes it with Hermes' own plugins.dashboard_auth.basic.hash_password (scrypt),
#   run by the Hermes runtime interpreter; the password travels over a pipe.
# - Writes/replaces HERMES_DASHBOARD_BASIC_AUTH_USERNAME and ..._PASSWORD_HASH in
#   ~/.hermes/.env, adds ..._SECRET only if unset (sessions survive restarts),
#   removes any plaintext ..._PASSWORD line, keeps the file 0600.
# - Prints key names only. Idempotent: re-run it to change the password.
#
# Then restart the dashboard agent so it re-reads .env:
#   launchctl kickstart -k gui/$(id -u)/com.projectarc.hermes-dashboard
#
# Overrides (non-standard installs / tests): HERMES_ENV_FILE, HERMES_AGENT_DIR,
# HERMES_RUNTIME_PYTHON.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="${1:-$(cd "$HERE/../.." && pwd)}"
ENV_FILE="${HERMES_ENV_FILE:-$HOME/.hermes/.env}"
AGENT="${HERMES_AGENT_DIR:-$HOME/.hermes/hermes-agent}"
ARC_PY="$REPO/.venv/bin/python"

[[ -x "$ARC_PY" ]] || { echo "error: $ARC_PY missing (run: uv sync in $REPO)" >&2; exit 1; }

if [[ -n "${HERMES_RUNTIME_PYTHON:-}" ]]; then
  RPY="$HERMES_RUNTIME_PYTHON"
else
  LAUNCHER="$AGENT/.hermes/bin/hermes"
  [[ -x "$LAUNCHER" ]] || { echo "error: Hermes launcher not found at $LAUNCHER" >&2; exit 1; }
  # The interpreter the `hermes` launcher itself runs (first element of its argv).
  RPY="$("$LAUNCHER" --print-runtime-command | "$ARC_PY" -c 'import json,sys; print(json.load(sys.stdin)[0])')"
fi
[[ -x "$RPY" ]] || { echo "error: Hermes runtime python not found ($RPY)" >&2; exit 1; }

default_user="${USER:-arc}"
read -r -p "Dashboard username [$default_user]: " username
username="${username:-$default_user}"

read -r -s -p "New dashboard password: " pw1; echo
read -r -s -p "Repeat password: " pw2; echo
if [[ -z "$pw1" ]]; then
  echo "error: empty password" >&2; exit 1
fi
if [[ "$pw1" != "$pw2" ]]; then
  unset pw1 pw2
  echo "error: passwords do not match" >&2; exit 1
fi
if (( ${#pw1} < 12 )); then
  unset pw1 pw2
  echo "error: use at least 12 characters" >&2; exit 1
fi

# printf is a shell builtin: the password is written to the pipe, not to any argv.
hash="$(printf '%s\n' "$pw1" | "$RPY" -I "$HERE/hash_password.py" "$AGENT")"
unset pw1 pw2

umask 077
printf '%s\n%s\n' "$username" "$hash" | (cd "$REPO" && "$ARC_PY" -m arc.remote.envfile "$ENV_FILE")
unset hash
echo "updated $ENV_FILE (mode $(stat -f %Lp "$ENV_FILE" 2>/dev/null || stat -c %a "$ENV_FILE"))"
echo "restart the dashboard to apply: launchctl kickstart -k gui/$(id -u)/com.projectarc.hermes-dashboard"
