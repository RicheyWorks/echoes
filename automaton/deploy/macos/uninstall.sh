#!/usr/bin/env bash
# Reverse what install.sh did. Keeps the env file + the DB - delete by
# hand if you want them gone.
set -euo pipefail

AGENT_DIR="${HOME}/Library/LaunchAgents"
UID_=$(id -u)

for unit in com.automaton.worker com.automaton.scheduler com.automaton.ui; do
  dest="${AGENT_DIR}/${unit}.plist"
  if [ -f "${dest}" ]; then
    launchctl bootout "gui/${UID_}" "${dest}" 2>/dev/null || true
    rm "${dest}"
    echo "  removed: ${dest}"
  fi
done

cat <<MSG

  removed automaton launchd agents.
  state preserved at:
    ~/Library/Application Support/automaton/   (DB + env file)
    ~/Library/Logs/automaton/                  (rotated logs)
  delete those manually if you want to start clean.
MSG
