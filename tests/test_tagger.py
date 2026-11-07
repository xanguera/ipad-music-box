import time

from musicbox import tagger
from musicbox.db import Library

SONGS = [
    {"id": "aaaaaaaaaaa", "title": "Queen – Bohemian Rhapsody", "channel": "Queen Official"},
    {"id": "bbbbbbbbbbb", "title": "Baby Shark Dance", "channel": "Pinkfong"},
    {"id": "ccccccccccc", "title": "Something unclear", "channel": ""},
]


def test_answer_is_read_line_by_line():
    prompts = []

    def ask(prompt):
        prompts.append(prompt)
        # Shaped like a real answer, plus the stray lines a hook or the model may add.
        return ("some-hook: a message on the way\n"
                "1: queen, Rock, related, 70s,  epic \n"
                "2. pinkfong, kids, playful, pop, toddlers, dance, extra\n"
                "7: out of range\n"
                "Hope this helps!\n")

    got = tagger.tags_for(SONGS, ["rock", "related", "baroque"], ask)
    assert got == {"aaaaaaaaaaa": ["queen", "rock", "70s", "epic"],
                   "bbbbbbbbbbb": ["pinkfong", "kids", "playful", "pop", "toddlers", "dance"]}
    assert "Existing tags: rock, baroque" in prompts[0]
    assert "1. Title: Queen – Bohemian Rhapsody | Channel: Queen Official" in prompts[0]
    assert "3. Title: Something unclear | Channel: -" in prompts[0]


def test_tagger_adds_tags_keeps_the_admins_and_retries_what_was_left_out(tmp_path):
    lib = Library(tmp_path / "l.db")
    for s in SONGS:
        lib.add_song(s["id"], s["title"], s["channel"])
    lib.set_song_tags("aaaaaaaaaaa", "dance")

    def ask(prompt):
        # Answer by title, since the songs may arrive in one call or several.
        return "\n".join(f"{n}: queen, rock" if "Queen" in line else f"{n}: kids" if "Shark" in line else ""
                         for n, line in enumerate(prompt.split("Songs:\n")[1].splitlines(), 1))  # song 3 left out

    t = tagger.Tagger(lib, ask)
    t.catch_up()
    for _ in range(100):
        if lib.songs_not_looked_up() == ["ccccccccccc"]:
            break
        time.sleep(0.02)
    assert lib.songs_not_looked_up() == ["ccccccccccc"]  # asked again at the next start
    assert lib.get_song("aaaaaaaaaaa")["tags"] == ["dance", "queen", "rock"]
    assert lib.get_song("bbbbbbbbbbb")["tags"] == ["kids"]


def test_a_failed_call_tags_nothing_and_keeps_the_songs_for_later(tmp_path):
    lib = Library(tmp_path / "l.db")
    lib.add_song("aaaaaaaaaaa", "Queen – Bohemian Rhapsody")

    def ask(prompt):
        raise RuntimeError("not logged in")

    t = tagger.Tagger(lib, ask)
    t._tag([lib.get_song("aaaaaaaaaaa")])
    assert lib.songs_not_looked_up() == ["aaaaaaaaaaa"] and lib.get_song("aaaaaaaaaaa")["tags"] == []


def test_musicbox_claude_setting_is_the_only_program_used(monkeypatch):
    import pytest
    monkeypatch.setenv("MUSICBOX_CLAUDE", "/nonexistent/claude")
    with pytest.raises(RuntimeError, match="not installed"):
        tagger.claude_path()
