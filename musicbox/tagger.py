"""Automatic tags, from Claude: a bare `claude -p` call reads the titles and channels of a few
songs and answers a handful of short tags for each (artist, genre, era, mood, "kids").

The call carries none of the user's Claude Code setup (no tools, MCP servers, settings or
memory), only a short instruction and the songs. It is given the tags already in use, so it
reuses "baroque" instead of inventing "baroque music". Songs are sent in small batches.
"""

import logging
import os
import queue
import re
import shutil
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from musicbox.db import Library

log = logging.getLogger(__name__)

MODEL = "claude-haiku-5-5"
BATCH = 10  # songs per call
MAX_TAGS = 6
INSTRUCTION = (
    "You tag music videos for a family music player used by children. For each song, reply with "
    "3 to 6 short lowercase tags: the main artist or group, the genre, the era or style, the mood, "
    "and 'kids' if it is made for children. Reuse tags from the existing list when they fit. "
    "Answer one line per song, as the song's number, a colon, then the tags separated by commas. "
    "Nothing else."
)
RESERVED = {"related"}  # set by the player itself, for songs it added; never by Claude
ANSWER_LINE = re.compile(r"^\s*(\d+)\s*[.:)]\s*(.+)$")

Ask = Callable[[str], str]


def claude_path() -> str:
    """The claude program: MUSICBOX_CLAUDE if set (and only that), else the installed one, not a
    terminal's wrapper of it."""
    chosen = os.environ.get("MUSICBOX_CLAUDE")
    for p in ([chosen] if chosen else [str(Path.home() / ".local/bin/claude"), shutil.which("claude")]):
        if p and Path(p).exists():
            return p
    raise RuntimeError("Claude Code (claude) is not installed")


def ask_claude(prompt: str) -> str:
    """One bare claude -p call: no tools, MCP servers, settings, slash commands or saved session."""
    cmd = [
        claude_path(), "-p", "--output-format", "text", "--model", MODEL,
        "--tools", "", "--strict-mcp-config", "--setting-sources", "", "--disable-slash-commands",
        "--no-session-persistence", "--exclude-dynamic-system-prompt-sections",
        "--system-prompt", INSTRUCTION, prompt,
    ]
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=180, stdin=subprocess.DEVNULL)
    if done.returncode != 0:
        raise RuntimeError(f"claude failed: {(done.stderr or done.stdout).strip()[-300:]}")
    return done.stdout


def tags_for(songs: list[dict], existing: list[str], ask: Ask = ask_claude) -> dict[str, list[str]]:
    """{song id: [tags]} for a batch of songs ({id, title, channel}). A song missing from the
    answer gets no entry."""
    existing = [t for t in existing if t.lower() not in RESERVED]
    lines = [f"Existing tags: {', '.join(existing[:300]) or '(none yet)'}", "Songs:"]
    for n, s in enumerate(songs, 1):
        lines.append(f"{n}. Title: {s['title']} | Channel: {s['channel'] or '-'}")
    out = {}
    for line in ask("\n".join(lines)).splitlines():
        m = ANSWER_LINE.match(line)
        if not m or not 1 <= int(m[1]) <= len(songs):
            continue  # anything else Claude or a hook printed
        tags = [" ".join(t.split()).strip(" .'\"").lower() for t in m[2].split(",")]
        out[songs[int(m[1]) - 1]["id"]] = [t for t in tags if t and len(t) <= 40 and t not in RESERVED][:MAX_TAGS]
    return out


class Tagger:
    """Tags new songs in a background thread, a few at a time."""

    def __init__(self, library: Library, ask: Ask = ask_claude) -> None:
        self.library = library
        self.ask = ask
        self.queue = queue.Queue()
        self.thread = threading.Thread(target=self._run, name="tagger", daemon=True)
        self.thread.start()

    def request(self, song_id: str) -> None:
        self.queue.put(song_id)

    def catch_up(self) -> None:
        """Queue every song never tagged (songs from before the tagger, or from a crash)."""
        for song_id in self.library.songs_not_looked_up():
            self.request(song_id)

    def _run(self) -> None:
        while True:
            ids = [self.queue.get()]
            while len(ids) < BATCH:  # whatever else is waiting goes in the same call
                try:
                    ids.append(self.queue.get_nowait())
                except queue.Empty:
                    break
            try:
                songs = [s for s in (self.library.get_song(i) for i in dict.fromkeys(ids)) if s]
                if songs:
                    self._tag(songs)
            except Exception:  # e.g. the database stayed locked: keep the thread alive for later songs
                log.exception("tagging failed")

    def _tag(self, songs: list[dict]) -> None:
        existing = [t["name"] for t in self.library.all_tags()]
        try:
            found = tags_for(songs, existing, self.ask)
        except Exception as e:  # not logged in, offline...: try again at the next start
            log.warning("tagging %d songs failed: %s", len(songs), e)
            return
        for s in songs:
            tags = found.get(s["id"])
            if tags is None:
                continue  # left out of the answer: tried again at the next start
            if tags:
                self.library.add_song_tags(s["id"], tags)
            self.library.mark_looked_up(s["id"])
            log.info("tagged %s: %s", s["id"], ", ".join(tags))
