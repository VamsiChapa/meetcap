#!/usr/bin/env bash
# Removes the login item, the command and the virtualenv. Settings, keys and your meetings are kept
# unless --purge is given (meetings in your storage folder or Azure are never deleted). OBS and ffmpeg stay.
set -euo pipefail

STATE="$HOME/Library/Application Support/meetcap"
AGENTS="$HOME/Library/LaunchAgents"
for f in "$AGENTS"/*.plist; do
  [[ -e "$f" ]] || continue
  if grep -q "meetcap/venv/bin/meetcap" "$f" 2>/dev/null; then
    launchctl bootout "gui/$(id -u)" "$f" 2>/dev/null || true
    rm -f "$f"
  fi
done
rm -f "$(brew --prefix 2>/dev/null || echo /opt/homebrew)/bin/meetcap"
rm -rf "$STATE/venv" "$STATE/bin"

if [[ "${1:-}" == "--purge" ]]; then
  "$(command -v security)" delete-generic-password -s meetcap >/dev/null 2>&1 || true
  while "$(command -v security)" delete-generic-password -s meetcap >/dev/null 2>&1; do :; done
  rm -rf "$STATE" "$HOME/Library/Logs/meetcap"
  echo "Uninstalled and removed settings, keys, logs and the local notes copy. Your meetings folder was not touched."
else
  echo "Uninstalled. Settings and keys kept in: $STATE (use --purge to remove them)."
fi
