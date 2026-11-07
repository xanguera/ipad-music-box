#!/bin/bash
# Stop and remove the music box launchd agents. Keeps the library and downloaded songs in data/.
set -eu
for label in org.musicbox.server org.musicbox.update; do
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/$label.plist"
done
echo "Removed."
