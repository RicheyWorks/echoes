#!/usr/bin/env bash
# Install automaton's launchd agents into the current user's
# ~/Library/LaunchAgents/. Idempotent: re-run after editing the env
# file to refresh the in-plist EnvironmentVariables.
#
# Usage:
#   ./install.sh                # uses the automaton binary on PATH
#   ./install.sh /opt/homebrew  # specify prefix (where automaton lives)
set -euo pipefail

PREFIX="${1:-${HOMEBREW_PREFIX:-/usr/local}}"

if [ ! -x "${PREFIX}/bin/automaton" ]; then
  # Fall back to whatever automaton is on PATH
  if command -v automaton >/dev/null 2>&1; then
    PREFIX="$(dirname "$(dirname "$(command -v automaton)")")"
    echo "  using automaton at ${PREFIX}/bin/automaton"
  else
    echo "error: ${PREFIX}/bin/automaton not found and 'automaton' not on PATH"
    echo "       install automaton first (pip install -e . or brew install)"
    exit 1
  fi
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOMEDIR="${HOME}"
APP_SUPPORT="${HOMEDIR}/Library/Application Support/automaton"
LOG_DIR="${HOMEDIR}/Library/Logs/automaton"
AGENT_DIR="${HOMEDIR}/Library/LaunchAgents"
ENV_FILE="${APP_SUPPORT}/automaton.env"

mkdir -p "${APP_SUPPORT}" "${LOG_DIR}" "${AGENT_DIR}"

# Drop an env-file template if one isn't there yet. Users edit this and
# re-run install.sh to push the values into the plist EnvironmentVariables.
if [ ! -f "${ENV_FILE}" ]; then
  cat > "${ENV_FILE}" <<EOF
# automaton config for this user. Edit, then re-run deploy/macos/install.sh
# to refresh the launchd agent environment.
AUTOMATON_DB=${APP_SUPPORT}/automaton.db
AUTOMATON_LOG_FILE=${LOG_DIR}/automaton.log
AUTOMATON_LOG_LEVEL=INFO
AUTOMATON_LOG_FORMAT=json

# Generate a token with:
#   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
# AUTOMATON_TOKEN=

# Optional: notifications. See README + deploy/litestream for backup.
# AUTOMATON_NOTIFY_ON_FAILURE=ntfy://your-host/topic
# AUTOMATON_NOTIFY_QUIET_HOURS=22:00-07:00
EOF
  chmod 600 "${ENV_FILE}"
  echo "  created env file: ${ENV_FILE}"
fi

# Install (or refresh) each plist with @PREFIX@ / @HOME@ substituted +
# the user's env-file values folded into EnvironmentVariables.
substitute_plist() {
  local src="$1"
  local dest="$2"
  python3 - "$src" "$dest" "$PREFIX" "$HOMEDIR" "$ENV_FILE" <<'PY'
import plistlib, os, sys, re
src, dest, prefix, homedir, envfile = sys.argv[1:6]
with open(src, "rb") as fh:
    raw = fh.read()
text = raw.decode("utf-8").replace("@PREFIX@", prefix).replace("@HOME@", homedir)
doc = plistlib.loads(text.encode("utf-8"))
# Fold env file into EnvironmentVariables (env file wins).
env = doc.get("EnvironmentVariables", {}) or {}
if os.path.exists(envfile):
    for line in open(envfile, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")
doc["EnvironmentVariables"] = env
with open(dest, "wb") as out:
    plistlib.dump(doc, out)
PY
  chmod 600 "$dest"
}

for unit in com.automaton.worker com.automaton.scheduler com.automaton.ui; do
  src="${HERE}/${unit}.plist"
  dest="${AGENT_DIR}/${unit}.plist"

  # If already loaded, unload first so launchctl picks up the refresh.
  if launchctl print "gui/$(id -u)/${unit}" >/dev/null 2>&1; then
    launchctl bootout "gui/$(id -u)" "${dest}" 2>/dev/null || true
  fi

  substitute_plist "${src}" "${dest}"
  launchctl bootstrap "gui/$(id -u)" "${dest}"
  launchctl enable "gui/$(id -u)/${unit}"
  echo "  installed: ${unit}"
done

cat <<MSG

  installed automaton launchd agents for $(whoami)
  app dir:   ${APP_SUPPORT}
  log dir:   ${LOG_DIR}
  agents:    ${AGENT_DIR}/com.automaton.*.plist

  edit the env file and re-run this script to refresh:
    ${EDITOR:-vim} '${ENV_FILE}' && ${HERE}/install.sh ${PREFIX}

  status:
    launchctl print gui/\$(id -u)/com.automaton.worker
  tail logs:
    tail -F ${LOG_DIR}/worker.{out,err}.log
  UI:
    open http://127.0.0.1:8080/
MSG
