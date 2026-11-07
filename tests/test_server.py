import time

import pytest

from musicbox import youtube
from musicbox.server import create_app

SONG = "ZbZSe6N_BXs"


@pytest.fixture
def app(tmp_path, monkeypatch):
    # No network in tests: a fake download writes a small file.
    def fake_prepare(self, song_id):
        d = self.song_dir(song_id)
        d.mkdir(parents=True)
        (d / "seg_00000.ts").write_bytes(b"\x00" * 10_000)
        (d / "index.m3u8").write_text("#EXTM3U\n#EXTINF:4.0,\nseg_00000.ts\n#EXT-X-ENDLIST\n")

    monkeypatch.setattr(youtube.MediaCache, "_prepare", fake_prepare)
    from musicbox import server
    monkeypatch.setattr(server, "WRONG_PIN_DELAY", 0)  # no waiting after a wrong PIN in tests
    app = create_app(tmp_path, online=False)
    lib = app.extensions["musicbox"]["library"]
    lib.add_song(SONG, "Happy", "Pharrell Williams", 233)
    lib.set_song_tags(SONG, "dance")
    lib.add_song("dddddddddd2", "Shake It Off", "Taylor Swift", 219)
    lib.set_song_tags("dddddddddd2", "dance, pop")  # "pop" is on one song only
    pid = lib.create_playlist("Car songs")
    lib.add_to_playlist(pid, SONG)
    lib.add_to_playlist(pid, "dddddddddd2")  # every song belongs to a playlist
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def login(client):
    return client.post("/admin/login", data={"pin": "1234"})


def test_full_page_and_fragment(client):
    full = client.get("/").get_data(as_text=True)
    assert "<video" in full and "Car songs" in full and "dance" in full
    frag = client.get("/", headers={"X-Fragment": "1"}).get_data(as_text=True)
    assert "<video" not in frag and "Car songs" in frag


@pytest.mark.parametrize("url", ["/loved", "/recent", "/tags?t=dance", "/songs", "/songs?order=added", "/playlist/1", "/playlists"])
def test_browse_pages_list_the_song(client, url):
    client.post(f"/api/love/{SONG}")
    client.post(f"/api/played/{SONG}")
    r = client.get(url, headers={"X-Fragment": "1"})
    assert r.status_code == 200
    assert f'data-id="{SONG}"' in r.get_data(as_text=True) or url == "/playlists"


def test_paging_more_link(client, app):
    lib = app.extensions["musicbox"]["library"]
    for i in range(35):
        lib.add_song(f"x{i:010d}", f"Song {i:02d}", "Band")
    page1 = client.get("/songs", headers={"X-Fragment": "1"}).get_data(as_text=True)
    assert page1.count('class="song"') == 30 and 'class="more"' in page1
    rows = client.get("/songs?offset=30&append=1").get_data(as_text=True)
    assert rows.count('class="song"') == 7 and "<html" not in rows and 'class="more"' not in rows


def test_prepare_then_serves_playlist_and_chunks(client):
    assert client.get(f"/media/{SONG}/index.m3u8").status_code == 404
    assert client.post(f"/api/prepare/{SONG}").get_json()["status"] in ("queued", "downloading", "ready")
    for _ in range(50):
        if client.get(f"/api/status/{SONG}").get_json()["status"] == "ready":
            break
        time.sleep(0.05)
    pl = client.get(f"/media/{SONG}/index.m3u8")
    assert pl.status_code == 200 and "seg_00000.ts" in pl.get_data(as_text=True)
    assert pl.headers["Content-Type"] == "application/vnd.apple.mpegurl" and pl.headers["Cache-Control"] == "no-store"
    r = client.get(f"/media/{SONG}/seg_00000.ts", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.data) == 100 and r.headers["Content-Type"] == "video/mp2t"
    assert client.get(f"/media/{SONG}/..%2Fsecret_key").status_code == 404
    assert client.get(f"/media/{SONG}/other.txt").status_code == 404


def test_prepare_unknown_song_is_404(client):
    assert client.post("/api/prepare/AAAAAAAAAAA").status_code == 404


def test_admin_needs_pin(client):
    assert client.get("/admin/").status_code == 302
    r = client.post("/admin/login", data={"pin": "nope"})
    assert "Wrong PIN" in r.get_data(as_text=True)
    assert login(client).status_code == 302
    assert "Add music" in client.get("/admin/").get_data(as_text=True)


def test_admin_add_tag_and_playlist(client, app):
    login(client)
    r = client.post("/admin/add", data={
        "id": "jv-pYB0Qw9A", "title_jv-pYB0Qw9A": "Happy (Lyrics)", "channel_jv-pYB0Qw9A": "Someone",
        "duration_jv-pYB0Qw9A": "238", "tags": "dance, lyrics", "new_playlist": "Party",
    })
    assert r.status_code == 302
    lib = app.extensions["musicbox"]["library"]
    s = lib.get_song("jv-pYB0Qw9A")
    assert s["duration"] == 238 and s["tags"] == ["dance", "lyrics"]
    party = [p for p in lib.playlists() if p["name"] == "Party"][0]
    assert party["count"] == 1
    # Edit the song's tags from its page.
    client.post("/admin/song/jv-pYB0Qw9A", data={"action": "save", "title": "Happy", "channel": "X", "tags": "kids"})
    assert lib.get_song("jv-pYB0Qw9A")["tags"] == ["kids"]


def test_admin_pages_render(client):
    login(client)
    for url in ["/admin/", "/admin/library", "/admin/library?q=happy", f"/admin/song/{SONG}",
                "/admin/playlists", "/admin/playlist/1", "/admin/tags", "/admin/settings"]:
        assert client.get(url).status_code == 200, url


def test_change_pin(client):
    login(client)
    client.post("/admin/settings", data={"pin": "9876", "cache_gb": "5"})
    client.post("/admin/logout")
    assert "Wrong PIN" in login(client).get_data(as_text=True)
    assert client.post("/admin/login", data={"pin": "9876"}).status_code == 302


def test_video_quality_setting(client, app):
    cache = app.extensions["musicbox"]["cache"]
    assert cache.max_height == 480
    login(client)
    client.post("/admin/settings", data={"video_height": "360"})
    assert cache.max_height == 360
    client.post("/admin/settings", data={"video_height": "999"})  # not a choice: ignored
    assert cache.max_height == 360
    assert app.extensions["musicbox"]["settings"]["video_height"] == 360


def test_growing_playlist_asks_players_to_start_at_the_beginning(client, app):
    cache = app.extensions["musicbox"]["cache"]
    d = cache.song_dir("jv-pYB0Qw9A")
    d.mkdir(parents=True)
    (d / "index.m3u8").write_text("#EXTM3U\n#EXT-X-PLAYLIST-TYPE:EVENT\n#EXTINF:4.0,\nseg_00000.ts\n")
    text = client.get("/media/jv-pYB0Qw9A/index.m3u8").get_data(as_text=True)
    assert text.startswith("#EXTM3U\n#EXT-X-START:TIME-OFFSET=0\n")
    done = client.get(f"/media/{SONG}/index.m3u8")  # not prepared yet in this test
    assert done.status_code == 404


def test_debug_log_is_off_until_switched_on(client, app, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="musicbox.server")
    client.post("/api/log?e=stuck&id=x")
    client.get("/api/status/jv-pYB0Qw9A")
    assert "stuck" not in caplog.text and "/api/status" not in caplog.text
    assert 'data-debug="0"' in client.get("/").get_data(as_text=True)
    login(client)
    client.post("/admin/settings", data={"debug_log": "on"})
    client.post("/api/log?e=stuck&id=x")
    client.get("/api/status/jv-pYB0Qw9A")
    assert "e=stuck" in caplog.text and "GET /api/status/jv-pYB0Qw9A" in caplog.text
    assert 'data-debug="1"' in client.get("/").get_data(as_text=True)


def test_home_offers_lists_and_tags_but_no_search(client):
    html = client.get("/").get_data(as_text=True)
    for part in ['id="b-refresh"', 'href="/loved"', 'href="/recent"', 'href="/playlist/1"', "/tags?t=dance", "Lists &amp; tags", "Admin"]:
        assert part in html, part
    assert 'type="search"' not in html
    assert client.get("/search?q=happy").status_code == 404


def test_love_a_song(client):
    assert client.get(f"/api/love/{SONG}").get_json() == {"loved": False}
    assert client.post(f"/api/love/{SONG}").get_json() == {"loved": True}
    assert "Happy" in client.get("/loved").get_data(as_text=True)
    assert client.post(f"/api/love/{SONG}").get_json() == {"loved": False}
    assert client.post("/api/love/nosuchsong1").status_code == 404


def fake_related(song_id, limit=25):
    return [
        {"id": "rrrrrrrrrr1", "title": "Short one", "channel": "C", "duration": 200},
        {"id": "rrrrrrrrrr2", "title": "A concert", "channel": "C", "duration": 3 * 3600},
        {"id": "rrrrrrrrrr3", "title": "Already queued", "channel": "C", "duration": 180},
        {"id": SONG, "title": "Played just now", "channel": "C", "duration": 233},
        {"id": "rrrrrrrrrr4", "title": "No length", "channel": "C", "duration": None},
    ]


def test_related_songs_skip_long_queued_and_recent(client, app, monkeypatch):
    monkeypatch.setattr(youtube, "related", fake_related)
    client.post(f"/api/played/{SONG}")
    got = client.get(f"/api/related/{SONG}?not=rrrrrrrrrr3").get_json()["songs"]
    assert [s["id"] for s in got] == ["rrrrrrrrrr1"]
    lib = app.extensions["musicbox"]["library"]
    assert lib.get_song("rrrrrrrrrr1")["tags"] == ["related"]  # joins the library, marked
    related = [p for p in lib.playlists() if p["name"] == "Related"]
    assert related and [s["id"] for s in lib.playlist_songs(related[0]["id"])] == ["rrrrrrrrrr1"]


def test_related_songs_can_be_switched_off(client, monkeypatch):
    monkeypatch.setattr(youtube, "related", fake_related)
    login(client)
    client.post("/admin/settings", data={})  # every box unticked
    assert client.get(f"/api/related/{SONG}").get_json() == {"songs": []}


def test_related_songs_when_youtube_fails(client, monkeypatch):
    def broken(song_id, limit=25):
        raise RuntimeError("YouTube is down")
    monkeypatch.setattr(youtube, "related", broken)
    assert client.get(f"/api/related/{SONG}").get_json() == {"songs": []}


def test_tags_on_a_single_song_are_not_offered(client):
    home = client.get("/").get_data(as_text=True)
    assert "/tags?t=dance" in home and "/tags?t=pop" not in home
    narrowing = client.get("/tags?t=dance").get_data(as_text=True)
    assert "t=pop" not in narrowing


def test_adding_songs_needs_a_playlist(client, app):
    login(client)
    lib = app.extensions["musicbox"]["library"]
    form = {"id": "jv-pYB0Qw9A", "title_jv-pYB0Qw9A": "Happy (Lyrics)", "channel_jv-pYB0Qw9A": "Someone"}
    client.post("/admin/add", data=form)  # no playlist chosen
    assert lib.get_song("jv-pYB0Qw9A") is None
    client.post("/admin/add", data=form | {"playlist": "999"})  # a playlist that does not exist
    assert lib.get_song("jv-pYB0Qw9A") is None
    client.post("/admin/add-link", data={"url": "https://www.youtube.com/watch?v=jv-pYB0Qw9A"})
    assert lib.get_song("jv-pYB0Qw9A") is None  # refused before YouTube is even asked
    client.post("/admin/add", data=form | {"playlist": "1"})
    assert [s["id"] for s in lib.playlist_songs(1)][-1] == "jv-pYB0Qw9A"


def test_removing_a_song_from_its_last_playlist_deletes_it_and_its_file(client, app):
    login(client)
    lib, cache = app.extensions["musicbox"]["library"], app.extensions["musicbox"]["cache"]
    other = lib.create_playlist("Dance")
    lib.add_to_playlist(other, SONG)
    client.post(f"/api/prepare/{SONG}")
    for _ in range(50):
        if cache.status(SONG) == "ready":
            break
        time.sleep(0.05)
    item = {s["id"]: s["item_id"] for s in lib.playlist_songs(1)}[SONG]
    client.post("/admin/playlist/1", data={"action": "remove", "item": item})
    assert lib.get_song(SONG) and cache.status(SONG) == "ready"  # still in Dance
    client.post(f"/admin/playlist/{other}", data={"action": "delete"})
    assert lib.get_song(SONG) is None and not cache.song_dir(SONG).exists()
    assert lib.get_song("dddddddddd2")  # in Car songs, untouched


def test_youtube_search_pages_and_leaves_out_library_songs(client, monkeypatch):
    def fake_search(query, limit=20, offset=0):
        ids = [SONG] + [f"y{i:010d}" for i in range(1, 30)]  # SONG is already in the library
        return [{"id": i, "title": f"Song {i}", "channel": "C", "duration": 100} for i in ids[offset:offset + limit]]

    monkeypatch.setattr(youtube, "search", fake_search)
    login(client)
    page = client.get("/admin/youtube?q=happy").get_data(as_text=True)
    assert page.count('class="yt-row"') == 19 and f'data-id="{SONG}"' not in page
    assert 'data-hidden="1"' in page and 'data-next="20"' in page
    more = client.get("/admin/youtube?q=happy&offset=20&append=1").get_json()
    assert more["rows"].count('class="yt-row"') == 10 and more["hidden"] == 0
    assert more["next"] is None  # fewer than a full page: the end
    assert 'name="title_y0000000020"' in more["rows"]  # each row carries what the add form needs


def test_pin_pad_login(client, monkeypatch):
    from musicbox import server
    monkeypatch.setattr(server, "WRONG_PIN_DELAY", 0)
    assert client.get("/admin/session").get_json() == {"admin": False}
    assert client.post("/admin/login?json=1", data={"pin": "0000"}).get_json() == {"ok": False}
    assert client.get("/admin/session").get_json() == {"admin": False}
    assert client.post("/admin/login?json=1", data={"pin": "1234"}).get_json() == {"ok": True}
    assert client.get("/admin/session").get_json() == {"admin": True}
    assert client.get("/admin/").status_code == 200


def test_pin_must_be_digits_for_the_pad(client, app):
    login(client)
    client.post("/admin/settings", data={"pin": "abcd"})
    assert app.extensions["musicbox"]["settings"]["pin"] == "1234"
    client.post("/admin/settings", data={"pin": "246810"})
    assert app.extensions["musicbox"]["settings"]["pin"] == "246810"
