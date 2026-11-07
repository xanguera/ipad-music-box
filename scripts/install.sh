#!/bin/bash
# Install the music box as two launchd agents for this user:
#   org.musicbox.server  starts the server at login and restarts it if it stops
#   org.musicbox.update  updates yt-dlp every Monday at 04:00, then restarts the server
# Run again at any time to reinstall. Remove with scripts/uninstall.sh.
# Written for macOS bash 3.2.
set -eu

REPO="$(cd "$(dirname "$0")/.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
LOGS="$HOME/Library/Logs"
UID_NUM="$(id -u)"
mkdir -p "$AGENTS" "$LOGS"

# Bake the repo path in; everything else goes through $HOME inside sh -c, since launchd expands nothing.
write_plist() {  # label, command, extra keys
  cat > "$AGENTS/$1.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$1</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>-c</string>
    <string>export PATH="/opt/homebrew/bin:/usr/local/bin:\$HOME/.local/bin:/usr/bin:/bin"; cd "$REPO" &amp;&amp; $2</string>
  </array>
  $3
</dict>
</plist>
PLIST
}

write_plist org.musicbox.server \
  'exec uv run --frozen --no-dev python -m musicbox >> "$HOME/Library/Logs/musicbox.log" 2>&amp;1' \
  '<key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>'

write_plist org.musicbox.update \
  'exec scripts/update-ytdlp.sh >> "$HOME/Library/Logs/musicbox.log" 2>&amp;1' \
  '<key>StartCalendarInterval</key><dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>4</integer><key>Minute</key><integer>0</integer></dict>'

plutil -lint "$AGENTS/org.musicbox.server.plist" "$AGENTS/org.musicbox.update.plist"

(cd "$REPO" && uv sync --frozen --no-dev)

for label in org.musicbox.server org.musicbox.update; do
  launchctl bootout "gui/$UID_NUM/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_NUM" "$AGENTS/$label.plist"
done

echo "Installed. Player: http://$(scutil --get LocalHostName).local:8080/   Admin: /admin/   Log: $LOGS/musicbox.log"
