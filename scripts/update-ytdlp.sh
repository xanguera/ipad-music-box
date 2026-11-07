#!/bin/bash
# Update yt-dlp to its newest release and restart the server. YouTube changes often,
# and an old yt-dlp is the usual reason downloads stop working.
set -eu
cd "$(dirname "$0")/.."
echo "$(date '+%Y-%m-%d %H:%M:%S') updating yt-dlp"
uv lock --upgrade-package yt-dlp --upgrade-package yt-dlp-ejs
uv sync --frozen --no-dev
uv run --frozen --no-dev yt-dlp --version
launchctl kickstart -k "gui/$(id -u)/org.musicbox.server" 2>/dev/null || true
