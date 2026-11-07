"""SQLite storage for songs, tags and playlists.

Every song belongs to at least one playlist: a song taken out of its last playlist, or whose
last playlist is deleted, leaves the library.

One connection per call keeps things simple and safe across the server's threads.
The library is small (thousands of songs at most), so plain LIKE search is fast enough
and, unlike full-text search, also matches the middle of a word ("ppy" finds "Happy").
"""

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS songs (
    id TEXT PRIMARY KEY,            -- YouTube video id
    title TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT '',
    duration INTEGER,               -- seconds
    added_at REAL NOT NULL,
    last_played_at REAL,
    play_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE
);
CREATE TABLE IF NOT EXISTS song_tags (
    song_id TEXT NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (song_id, tag_id)
);
CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS playlist_items (
    id INTEGER PRIMARY KEY,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    song_id TEXT NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
    position INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS playlist_items_by_list ON playlist_items(playlist_id, position);
-- No link to songs on purpose: a love outlives its song, so a song that leaves the library
-- (with its last playlist) and comes back later is loved again. Lists only show songs that exist.
CREATE TABLE IF NOT EXISTS loved (
    song_id TEXT PRIMARY KEY,
    loved_at REAL NOT NULL
);
"""
# Columns added after the first version; added to an existing database at start.
NEW_COLUMNS = {"songs": {"looked_up_at": "REAL"}}  # when the tagger asked MusicBrainz about the song

SONG_COLUMNS = "s.id, s.title, s.channel, s.duration, s.last_played_at, s.play_count"


class Library:
    def __init__(self, path: Path):
        self.path = path
        with self.connect() as c:
            c.executescript(SCHEMA)
            # Before 2026-11-07 evening a love was deleted with its song; keep loves from now on.
            old = c.execute("SELECT sql FROM sqlite_master WHERE name = 'loved'").fetchone()
            if old and "REFERENCES" in old[0]:
                c.execute("ALTER TABLE loved RENAME TO loved_old")
                c.executescript(SCHEMA)
                c.execute("INSERT INTO loved SELECT song_id, loved_at FROM loved_old")
                c.execute("DROP TABLE loved_old")
            for table, columns in NEW_COLUMNS.items():
                have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
                for name, kind in columns.items():
                    if name not in have:
                        c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # ---- songs ----------------------------------------------------------

    def add_song(self, song_id, title, channel="", duration=None):
        """Add a song, or refresh its title if it is already there. Returns True if new."""
        with self.connect() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO songs (id, title, channel, duration, added_at) VALUES (?, ?, ?, ?, ?)",
                (song_id, title, channel or "", duration, time.time()),
            )
            return cur.rowcount == 1

    def get_song(self, song_id):
        with self.connect() as c:
            row = c.execute(f"SELECT {SONG_COLUMNS} FROM songs s WHERE s.id = ?", (song_id,)).fetchone()
            return self._with_tags(c, [row])[0] if row else None

    def update_song(self, song_id, title, channel):
        with self.connect() as c:
            c.execute("UPDATE songs SET title = ?, channel = ? WHERE id = ?", (title, channel, song_id))

    def delete_song(self, song_id):
        with self.connect() as c:
            c.execute("DELETE FROM songs WHERE id = ?", (song_id,))
            self._drop_unused_tags(c)

    def mark_played(self, song_id):
        with self.connect() as c:
            c.execute(
                "UPDATE songs SET last_played_at = ?, play_count = play_count + 1 WHERE id = ?",
                (time.time(), song_id),
            )

    def search_songs(self, query="", tags=(), offset=0, limit=30, order="title"):
        """Songs that match every word of the query (in title, channel or a tag) and carry every tag."""
        where, params = [], []
        for word in query.split():
            where.append(
                "(s.title LIKE ? OR s.channel LIKE ? OR EXISTS ("
                " SELECT 1 FROM song_tags st JOIN tags t ON t.id = st.tag_id"
                " WHERE st.song_id = s.id AND t.name LIKE ?))"
            )
            like = f"%{word}%"
            params += [like, like, like]
        for tag in tags:
            where.append(
                "EXISTS (SELECT 1 FROM song_tags st JOIN tags t ON t.id = st.tag_id"
                " WHERE st.song_id = s.id AND t.name = ?)"
            )
            params.append(tag)
        sql = f"SELECT {SONG_COLUMNS} FROM songs s"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += {
            "title": " ORDER BY s.title COLLATE NOCASE",
            "recent": " ORDER BY s.last_played_at DESC",
            "added": " ORDER BY s.added_at DESC",
        }[order]
        sql += " LIMIT ? OFFSET ?"
        params += [limit, offset]
        with self.connect() as c:
            return self._with_tags(c, c.execute(sql, params).fetchall())

    def recently_played(self, limit=12):
        with self.connect() as c:
            rows = c.execute(
                f"SELECT {SONG_COLUMNS} FROM songs s WHERE s.last_played_at IS NOT NULL"
                " ORDER BY s.last_played_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return self._with_tags(c, rows)

    def songs_not_looked_up(self):
        with self.connect() as c:
            return [r[0] for r in c.execute("SELECT id FROM songs WHERE looked_up_at IS NULL ORDER BY added_at")]

    def mark_looked_up(self, song_id):
        with self.connect() as c:
            c.execute("UPDATE songs SET looked_up_at = ? WHERE id = ?", (time.time(), song_id))

    def played_since(self, since):
        """Ids of songs played after the given time."""
        with self.connect() as c:
            return {r[0] for r in c.execute("SELECT id FROM songs WHERE last_played_at > ?", (since,))}

    # ---- loved ----------------------------------------------------------

    def toggle_love(self, song_id):
        """Love a song, or stop loving it. Returns True if it is loved now."""
        with self.connect() as c:
            if c.execute("DELETE FROM loved WHERE song_id = ?", (song_id,)).rowcount:
                return False
            c.execute("INSERT INTO loved (song_id, loved_at) SELECT id, ? FROM songs WHERE id = ?",
                      (time.time(), song_id))
            return True

    def is_loved(self, song_id):
        with self.connect() as c:
            return c.execute("SELECT 1 FROM loved l JOIN songs s ON s.id = l.song_id WHERE l.song_id = ?",
                             (song_id,)).fetchone() is not None

    def loved_songs(self):
        """Newest love first."""
        with self.connect() as c:
            rows = c.execute(
                f"SELECT {SONG_COLUMNS} FROM songs s JOIN loved l ON l.song_id = s.id ORDER BY l.loved_at DESC"
            ).fetchall()
            return self._with_tags(c, rows)

    def count_loved(self):
        with self.connect() as c:
            return c.execute("SELECT COUNT(*) FROM loved l JOIN songs s ON s.id = l.song_id").fetchone()[0]

    def count_songs(self):
        with self.connect() as c:
            return c.execute("SELECT COUNT(*) FROM songs").fetchone()[0]

    # ---- tags -----------------------------------------------------------

    def set_song_tags(self, song_id, names):
        names = clean_tags(names)
        with self.connect() as c:
            c.execute("DELETE FROM song_tags WHERE song_id = ?", (song_id,))
            for name in names:
                c.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
                tag_id = c.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()[0]
                c.execute("INSERT OR IGNORE INTO song_tags VALUES (?, ?)", (song_id, tag_id))
            self._drop_unused_tags(c)

    def add_song_tags(self, song_id, names):
        song = self.get_song(song_id)
        if song:
            self.set_song_tags(song_id, song["tags"] + clean_tags(names))

    def all_tags(self):
        """Every tag with its number of songs, most used first."""
        with self.connect() as c:
            return [
                dict(r)
                for r in c.execute(
                    "SELECT t.name, COUNT(st.song_id) AS count FROM tags t"
                    " JOIN song_tags st ON st.tag_id = t.id GROUP BY t.id"
                    " ORDER BY count DESC, t.name COLLATE NOCASE"
                )
            ]

    def related_tags(self, chosen):
        """Other tags found on the songs that carry all the chosen tags, to narrow further."""
        if not chosen:
            return self.all_tags()
        marks = ",".join("?" * len(chosen))
        sql = (
            "SELECT t.name, COUNT(*) AS count FROM song_tags st JOIN tags t ON t.id = st.tag_id"
            " WHERE st.song_id IN ("
            "  SELECT st2.song_id FROM song_tags st2 JOIN tags t2 ON t2.id = st2.tag_id"
            f"  WHERE t2.name IN ({marks}) GROUP BY st2.song_id HAVING COUNT(DISTINCT t2.id) = ?)"
            f" AND t.name NOT IN ({marks}) GROUP BY t.id ORDER BY count DESC, t.name COLLATE NOCASE"
        )
        with self.connect() as c:
            return [dict(r) for r in c.execute(sql, [*chosen, len(chosen), *chosen])]

    def rename_tag(self, old, new):
        new = clean_tags([new])
        if not new:
            return
        new = new[0]
        with self.connect() as c:
            old_id = c.execute("SELECT id FROM tags WHERE name = ?", (old,)).fetchone()
            if not old_id:
                return
            target = c.execute("SELECT id FROM tags WHERE name = ?", (new,)).fetchone()
            if target and target[0] != old_id[0]:
                # Merge into the existing tag.
                c.execute(
                    "INSERT OR IGNORE INTO song_tags SELECT song_id, ? FROM song_tags WHERE tag_id = ?",
                    (target[0], old_id[0]),
                )
                c.execute("DELETE FROM tags WHERE id = ?", (old_id[0],))
            else:
                c.execute("UPDATE tags SET name = ? WHERE id = ?", (new, old_id[0]))

    def delete_tag(self, name):
        with self.connect() as c:
            c.execute("DELETE FROM tags WHERE name = ?", (name,))

    # ---- playlists ------------------------------------------------------

    def create_playlist(self, name):
        with self.connect() as c:
            return c.execute(
                "INSERT INTO playlists (name, created_at) VALUES (?, ?)", (name.strip(), time.time())
            ).lastrowid

    def playlists(self, query=""):
        with self.connect() as c:
            return [
                dict(r)
                for r in c.execute(
                    "SELECT p.id, p.name, COUNT(i.id) AS count,"
                    " (SELECT i2.song_id FROM playlist_items i2 WHERE i2.playlist_id = p.id"
                    "  ORDER BY i2.position LIMIT 1) AS cover"
                    " FROM playlists p LEFT JOIN playlist_items i ON i.playlist_id = p.id"
                    " WHERE p.name LIKE ? GROUP BY p.id ORDER BY p.name COLLATE NOCASE",
                    (f"%{query}%",),
                )
            ]

    def get_playlist(self, playlist_id):
        with self.connect() as c:
            row = c.execute("SELECT id, name FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
            return dict(row) if row else None

    def playlist_songs(self, playlist_id):
        with self.connect() as c:
            rows = c.execute(
                f"SELECT {SONG_COLUMNS}, i.id AS item_id,"
                " (SELECT COUNT(*) FROM playlist_items j WHERE j.song_id = s.id) AS lists"
                " FROM playlist_items i JOIN songs s ON s.id = i.song_id"
                " WHERE i.playlist_id = ? ORDER BY i.position",
                (playlist_id,),
            ).fetchall()
            return self._with_tags(c, rows)

    def rename_playlist(self, playlist_id, name):
        with self.connect() as c:
            c.execute("UPDATE playlists SET name = ? WHERE id = ?", (name.strip(), playlist_id))

    def delete_playlist(self, playlist_id):
        """Delete a playlist. Returns the ids of songs that were only in it, which leave the library."""
        with self.connect() as c:
            ids = [r[0] for r in c.execute("SELECT song_id FROM playlist_items WHERE playlist_id = ?", (playlist_id,))]
            c.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
            return self._delete_orphans(c, ids)

    def playlist_named(self, name):
        """The id of the playlist with this name, created if there is none."""
        with self.connect() as c:
            row = c.execute("SELECT id FROM playlists WHERE name = ? COLLATE NOCASE ORDER BY id", (name,)).fetchone()
        return row[0] if row else self.create_playlist(name)

    def add_to_playlist(self, playlist_id, song_id):
        with self.connect() as c:
            exists = c.execute(
                "SELECT 1 FROM playlist_items WHERE playlist_id = ? AND song_id = ?", (playlist_id, song_id)
            ).fetchone()
            if exists:
                return
            pos = c.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM playlist_items WHERE playlist_id = ?",
                (playlist_id,),
            ).fetchone()[0]
            c.execute(
                "INSERT INTO playlist_items (playlist_id, song_id, position) VALUES (?, ?, ?)",
                (playlist_id, song_id, pos),
            )

    def remove_from_playlist(self, playlist_id, item_id):
        """Take a song out of a playlist. Returns [its id] if that was its last playlist and it left
        the library, else []."""
        with self.connect() as c:
            row = c.execute("SELECT song_id FROM playlist_items WHERE id = ? AND playlist_id = ?",
                            (item_id, playlist_id)).fetchone()
            if not row:
                return []
            c.execute("DELETE FROM playlist_items WHERE id = ?", (item_id,))
            return self._delete_orphans(c, [row[0]])

    def move_in_playlist(self, playlist_id, item_id, step):
        """Move an item up (step -1) or down (step +1) by swapping it with its neighbour."""
        with self.connect() as c:
            ids = [
                r[0]
                for r in c.execute(
                    "SELECT id FROM playlist_items WHERE playlist_id = ? ORDER BY position", (playlist_id,)
                )
            ]
            if item_id not in ids:
                return
            i = ids.index(item_id)
            j = i + step
            if 0 <= j < len(ids):
                ids[i], ids[j] = ids[j], ids[i]
            for pos, iid in enumerate(ids):
                c.execute("UPDATE playlist_items SET position = ? WHERE id = ?", (pos, iid))

    def playlists_for_song(self, song_id):
        with self.connect() as c:
            return [
                dict(r)
                for r in c.execute(
                    "SELECT DISTINCT p.id, p.name FROM playlists p JOIN playlist_items i ON i.playlist_id = p.id"
                    " WHERE i.song_id = ? ORDER BY p.name COLLATE NOCASE",
                    (song_id,),
                )
            ]

    # ---- helpers --------------------------------------------------------

    @staticmethod
    def _with_tags(c, rows):
        songs = [dict(r) for r in rows]
        if not songs:
            return songs
        marks = ",".join("?" * len(songs))
        tags = {}
        for r in c.execute(
            "SELECT st.song_id, t.name FROM song_tags st JOIN tags t ON t.id = st.tag_id"
            f" WHERE st.song_id IN ({marks}) ORDER BY t.name COLLATE NOCASE",
            [s["id"] for s in songs],
        ):
            tags.setdefault(r[0], []).append(r[1])
        for s in songs:
            s["tags"] = tags.get(s["id"], [])
        return songs

    @classmethod
    def _delete_orphans(cls, c, song_ids):
        """Delete those of these songs that are in no playlist any more. Returns their ids."""
        gone = [i for i in dict.fromkeys(song_ids)
                if not c.execute("SELECT 1 FROM playlist_items WHERE song_id = ?", (i,)).fetchone()]
        for i in gone:
            c.execute("DELETE FROM songs WHERE id = ?", (i,))
        if gone:
            cls._drop_unused_tags(c)
        return gone

    @staticmethod
    def _drop_unused_tags(c):
        c.execute("DELETE FROM tags WHERE id NOT IN (SELECT tag_id FROM song_tags)")


def clean_tags(names):
    """Accept a list or a comma-separated string; trim, drop empties and case-insensitive duplicates."""
    if isinstance(names, str):
        names = names.split(",")
    out, seen = [], set()
    for n in names:
        n = " ".join(n.split())
        if n and n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out
