#!/usr/bin/env bash
# meetcap installer for macOS 14+ (Apple Silicon or Intel). Safe to re-run; re-run it to upgrade.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
STATE="$HOME/Library/Application Support/meetcap"
VENV="$STATE/venv"
BIN="$STATE/bin"
LOGS="$HOME/Library/Logs/meetcap"
LABEL="com.meetcap.agent"
AGENTS="$HOME/Library/LaunchAgents"
PLIST="$AGENTS/$LABEL.plist"

say()  { printf "\033[1m==> %s\033[0m\n" "$*"; }
die()  { printf "\033[31mError: %s\033[0m\n" "$*" >&2; exit 1; }

[[ "$(uname)" == "Darwin" ]] || die "macOS only."
major="$(sw_vers -productVersion | cut -d. -f1)"
(( major >= 14 )) || die "macOS 14 or newer is required (Core Audio process API)."
command -v brew >/dev/null || die "Install Homebrew first: https://brew.sh"
xcode-select -p >/dev/null 2>&1 || { xcode-select --install || true; die "Finish installing Xcode Command Line Tools, then re-run."; }

# Stop any meetcap login item (this one, or one from an older install under another name) so it doesn't
# drive OBS while we reconfigure things.
mkdir -p "$AGENTS"
for f in "$AGENTS"/*.plist; do
  [[ -e "$f" ]] || continue
  if grep -q "meetcap/venv/bin/meetcap" "$f" 2>/dev/null; then
    launchctl bootout "gui/$(id -u)" "$f" 2>/dev/null || true
    [[ "$f" == "$PLIST" ]] || rm -f "$f"
  fi
done

say "Installing dependencies (python@3.12, ffmpeg, OBS)"
brew list python@3.12 >/dev/null 2>&1 || brew install python@3.12
brew list ffmpeg      >/dev/null 2>&1 || brew install ffmpeg
brew list --cask obs  >/dev/null 2>&1 || [[ -d /Applications/OBS.app ]] || brew install --cask obs

say "Creating virtualenv in $VENV"
mkdir -p "$BIN" "$LOGS"
PY="$(brew --prefix python@3.12)/bin/python3.12"
[[ -d "$VENV" ]] || "$PY" -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
EXTRAS="ui"; [[ "$(uname -m)" == "arm64" ]] && EXTRAS="ui,local"   # local = Whisper on the Mac (Apple Silicon)
"$VENV/bin/pip" install --quiet --upgrade "$HERE[$EXTRAS]"

say "Building micwatch (tells meetcap when a meeting app is using the mic)"
swiftc -O -o "$BIN/micwatch" "$HERE/helpers/micwatch/main.swift"
"$BIN/micwatch" >/dev/null || die "micwatch failed to run"

LINK="$(brew --prefix)/bin/meetcap"
ln -sf "$VENV/bin/meetcap" "$LINK"
say "Command linked at $LINK"

"$VENV/bin/meetcap" init

say "Adding meetcap to your login items ($PLIST)"
cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$VENV/bin/meetcap</string>
    <string>menubar</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ProcessType</key><string>Interactive</string>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>ThrottleInterval</key><integer>20</integer>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$(brew --prefix)/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>StandardOutPath</key><string>$LOGS/stdout.log</string>
  <key>StandardErrorPath</key><string>$LOGS/stderr.log</string>
</dict>
</plist>
PLIST_EOF

say "Starting meetcap"
for _ in 1 2 3 4 5; do   # bootstrap right after bootout can fail with 'Input/output error' for a moment
  launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null && break
  sleep 2
done
launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || die "meetcap did not start; see $LOGS/stderr.log"

echo
say "Installed. The setup screen opens in your browser: pick where meetings go, set up OBS, choose a pet."
if [[ -t 0 ]]; then
  "$VENV/bin/meetcap" setup || echo "Open it again any time: meetcap setup"
else
  echo "Run: meetcap setup"
fi
