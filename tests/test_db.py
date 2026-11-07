import time

from musicbox.db import Library, clean_tags


def make(tmp_path):
    lib = Library(tmp_path / "t.db")
    lib.add_song("aaaaaaaaaaa", "Happy", "Pharrell Williams", 233)
    lib.add_song("bbbbbbbbbbb", "Let It Go", "Disney", 220)
    lib.add_song("ccccccccccc", "Shake It Off", "Taylor Swift", 219)
    lib.set_song_tags("aaaaaaaaaaa", "dance, Anna")
    lib.set_song_tags("bbbbbbbbbbb", ["Anna", "film"])
    lib.set_song_tags("ccccccccccc", "dance, pop")
    return lib


def ids(songs):
    return [s["id"][0] for s in songs]


def test_clean_tags():
    assert clean_tags(" dance ,Dance,, car  songs ") == ["dance", "car songs"]


def test_search_matches_title_artist_tag_and_word_middle(tmp_path):
    lib = make(tmp_path)
    assert ids(lib.search_songs("ppy")) == ["a"]
    assert ids(lib.search_songs("taylor")) == ["c"]
    assert ids(lib.search_songs("anna")) == ["a", "b"]  # tag
    assert ids(lib.search_songs("anna go")) == ["b"]  # every word must match


def test_tag_filter_and_related(tmp_path):
    lib = make(tmp_path)
    assert ids(lib.search_songs(tags=["dance"])) == ["a", "c"]
    assert ids(lib.search_songs(tags=["dance", "anna"])) == ["a"]  # tags match without case
    related = {t["name"]: t["count"] for t in lib.related_tags(["dance"])}
    assert related == {"Anna": 1, "pop": 1}
    assert lib.all_tags()[0] == {"name": "Anna", "count": 2} or lib.all_tags()[0]["count"] == 2


def test_unused_tags_disappear_and_rename_merges(tmp_path):
    lib = make(tmp_path)
    lib.set_song_tags("bbbbbbbbbbb", "Anna")
    assert "film" not in [t["name"] for t in lib.all_tags()]
    lib.rename_tag("pop", "dance")  # merge into existing tag
    assert {t["name"]: t["count"] for t in lib.all_tags()}["dance"] == 2
    assert "pop" not in [t["name"] for t in lib.all_tags()]


def test_playlist_order_and_moves(tmp_path):
    lib = make(tmp_path)
    pid = lib.create_playlist("Car")
    for sid in ("aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc", "aaaaaaaaaaa"):
        lib.add_to_playlist(pid, sid)
    songs = lib.playlist_songs(pid)
    assert ids(songs) == ["a", "b", "c"]  # no duplicates
    lib.move_in_playlist(pid, songs[2]["item_id"], -1)
    assert ids(lib.playlist_songs(pid)) == ["a", "c", "b"]
    lib.move_in_playlist(pid, songs[0]["item_id"], -1)  # already first: no change
    assert ids(lib.playlist_songs(pid)) == ["a", "c", "b"]
    assert lib.playlists()[0]["cover"] == "aaaaaaaaaaa"
    lib.delete_song("ccccccccccc")
    assert ids(lib.playlist_songs(pid)) == ["a", "b"]


def test_recently_played(tmp_path):
    lib = make(tmp_path)
    lib.mark_played("bbbbbbbbbbb")
    lib.mark_played("aaaaaaaaaaa")
    assert ids(lib.recently_played()) == ["a", "b"]
    assert lib.get_song("aaaaaaaaaaa")["play_count"] == 1


def test_love_toggles_and_lists_newest_first(tmp_path):
    lib = Library(tmp_path / "l.db")
    for sid in ("aaaaaaaaaaa", "bbbbbbbbbbb"):
        lib.add_song(sid, sid)
    assert lib.toggle_love("aaaaaaaaaaa") is True
    time.sleep(0.01)
    assert lib.toggle_love("bbbbbbbbbbb") is True
    assert [s["id"] for s in lib.loved_songs()] == ["bbbbbbbbbbb", "aaaaaaaaaaa"]
    assert lib.toggle_love("aaaaaaaaaaa") is False and not lib.is_loved("aaaaaaaaaaa")
    lib.delete_song("bbbbbbbbbbb")
    assert lib.loved_songs() == [] and lib.count_loved() == 0


def test_new_column_is_added_to_an_old_database(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE songs (id TEXT PRIMARY KEY, title TEXT NOT NULL, channel TEXT NOT NULL DEFAULT '',"
                " duration INTEGER, added_at REAL NOT NULL, last_played_at REAL, play_count INTEGER NOT NULL DEFAULT 0)")
    old.execute("INSERT INTO songs (id, title, added_at) VALUES ('aaaaaaaaaaa', 'Old', 0)")
    old.commit()
    old.close()
    lib = Library(path)
    assert lib.songs_not_looked_up() == ["aaaaaaaaaaa"]
    lib.mark_looked_up("aaaaaaaaaaa")
    assert lib.songs_not_looked_up() == []


def test_a_song_leaves_the_library_with_its_last_playlist(tmp_path):
    lib = Library(tmp_path / "l.db")
    a, b = lib.create_playlist("A"), lib.create_playlist("B")
    for sid in ("aaaaaaaaaaa", "bbbbbbbbbbb"):
        lib.add_song(sid, sid)
        lib.add_to_playlist(a, sid)
    lib.add_to_playlist(b, "bbbbbbbbbbb")
    lib.set_song_tags("aaaaaaaaaaa", "solo")
    items = {s["id"]: s for s in lib.playlist_songs(a)}
    assert items["aaaaaaaaaaa"]["lists"] == 1 and items["bbbbbbbbbbb"]["lists"] == 2
    assert lib.remove_from_playlist(a, items["bbbbbbbbbbb"]["item_id"]) == []  # still in B
    assert lib.get_song("bbbbbbbbbbb")
    assert lib.remove_from_playlist(a, items["aaaaaaaaaaa"]["item_id"]) == ["aaaaaaaaaaa"]
    assert lib.get_song("aaaaaaaaaaa") is None and lib.all_tags() == []  # its tag went too
    assert lib.delete_playlist(b) == ["bbbbbbbbbbb"] and lib.count_songs() == 0


def test_playlist_named_finds_or_creates(tmp_path):
    lib = Library(tmp_path / "l.db")
    first = lib.playlist_named("Related")
    assert lib.playlist_named("related") == first and len(lib.playlists()) == 1


def test_a_love_comes_back_with_its_song(tmp_path):
    lib = Library(tmp_path / "l.db")
    pid = lib.create_playlist("A")
    lib.add_song("aaaaaaaaaaa", "Song")
    lib.add_to_playlist(pid, "aaaaaaaaaaa")
    lib.toggle_love("aaaaaaaaaaa")
    lib.delete_playlist(pid)  # the song leaves the library with its last playlist
    assert lib.loved_songs() == [] and lib.count_loved() == 0
    lib.add_song("aaaaaaaaaaa", "Song")  # added again later
    assert lib.is_loved("aaaaaaaaaaa") and [s["id"] for s in lib.loved_songs()] == ["aaaaaaaaaaa"]


def test_loves_survive_reopening_and_the_upgrade_from_the_old_table(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    Library(path)  # today's tables...
    old = sqlite3.connect(path)  # ...with the loved table as it was before: deleted with its song
    old.executescript("DROP TABLE loved; CREATE TABLE loved (song_id TEXT PRIMARY KEY"
                      " REFERENCES songs(id) ON DELETE CASCADE, loved_at REAL NOT NULL);"
                      "INSERT INTO songs (id, title, added_at) VALUES ('aaaaaaaaaaa', 'Kept', 0);"
                      "INSERT INTO loved VALUES ('aaaaaaaaaaa', 1);")
    old.commit()
    old.close()
    lib = Library(path)  # what every server start does
    assert lib.is_loved("aaaaaaaaaaa")
    lib.delete_song("aaaaaaaaaaa")
    lib.add_song("aaaaaaaaaaa", "Kept")
    assert Library(path).is_loved("aaaaaaaaaaa")  # and again after another start
