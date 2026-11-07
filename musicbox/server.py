"""The web server: the kids' player pages, the admin pages, and the media files.

Player pages come in two forms from the same templates. A normal request gets the full page.
A request from the player's own script (header X-Fragment) gets only the part that changes,
so the page holding the video never reloads and the music keeps playing while the kids browse.
"""

import json
import logging
import os
import re
import secrets
import time
from functools import wraps
from pathlib import Path

from flask import (
    Blueprint,
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from . import youtube
from .db import Library, clean_tags
from .tagger import Tagger

PAGE = 30  # songs per page; small pages keep the old iPad's memory use low
WRONG_PIN_DELAY = 1.0  # seconds
YT_PAGE = 20  # YouTube search results per page
RELATED_COUNT = 5  # songs added each time the queue runs out
RELATED_PLAYLIST = "Related"  # where those songs are kept
RELATED_MAX_SECONDS = 20 * 60  # so a session of songs does not run into a two-hour concert
CHUNK = re.compile(r"^seg_\d{5}\.ts$")

log = logging.getLogger(__name__)


class Settings:
    """Small JSON file next to the database: the admin PIN, the cache rules and the video height."""

    DEFAULTS = {"pin": "1234", "cache_gb": 20, "keep_days": 7, "video_height": youtube.DEFAULT_HEIGHT,
                "debug_log": False, "related": True}

    def __init__(self, path: Path):
        self.path = path
        self.data = dict(self.DEFAULTS)
        if path.exists():
            self.data.update(json.loads(path.read_text()))
        else:
            self.save()

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value
        self.save()

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.replace(self.path)


def create_app(data_dir=None, online=True):
    """online=False (tests): no automatic tagging, which would ask MusicBrainz over the internet."""
    data_dir = Path(data_dir or os.environ.get("MUSICBOX_DATA", "data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    app = Flask(__name__)
    key_file = data_dir / "secret_key"
    if not key_file.exists():
        key_file.write_text(secrets.token_hex(32))
        key_file.chmod(0o600)
    app.secret_key = key_file.read_text().strip()
    app.config["PERMANENT_SESSION_LIFETIME"] = 30 * 24 * 3600
    # The admin forms carry no CSRF token: keep the login cookie off posts from other sites.
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

    settings = Settings(data_dir / "settings.json")
    library = Library(data_dir / "library.db")
    cache = youtube.MediaCache(data_dir / "media", data_dir / "thumbs", int(settings["cache_gb"] * 1e9),
                                 int(settings["video_height"]), float(settings["keep_days"]))
    if online:
        auto_tagger = Tagger(library)
        auto_tagger.catch_up()
        tag = auto_tagger.request
    else:
        def tag(_song_id):
            pass
    app.extensions["musicbox"] = {"library": library, "cache": cache, "settings": settings}

    @app.template_filter("duration")
    def duration_filter(seconds):
        if not seconds:
            return ""
        m, s = divmod(int(seconds), 60)
        h, m = divmod(m, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    @app.template_filter("size")
    def size_filter(n):
        return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"

    app.register_blueprint(player_pages(library))
    app.register_blueprint(media_routes(library, cache, settings, tag))
    app.register_blueprint(admin_pages(library, cache, settings, tag))

    @app.context_processor
    def debug_flag():
        return {"debug_log": settings["debug_log"]}

    @app.after_request
    def log_playback_requests(response):
        """With the debug log on (Settings): log what the player asks for (songs, chunks, status),
        so a stall can be traced afterwards."""
        if (settings["debug_log"] and request.path.startswith(("/media/", "/api/"))
                and request.path != "/api/log"):
            log.info("%s %s %s%s -> %s %s", request.remote_addr, request.method, request.path,
                     f" range={request.headers['Range']}" if "Range" in request.headers else "",
                     response.status_code, response.content_length or "")
        return response
    return app


# ---------------------------------------------------------------------------
# Player (the kids' side)
# ---------------------------------------------------------------------------


def player_pages(library):
    bp = Blueprint("player", __name__)

    def page(template, **ctx):
        fragment = request.headers.get("X-Fragment") == "1"
        layout = "fragment.html" if fragment else "shell.html"
        return render_template(template, layout=layout, **ctx)

    def shared(tags):
        """Only tags on more than one song: a tag that leads to a single song is no use for browsing."""
        return [t for t in tags if t["count"] > 1]

    def song_list(songs, offset, more_url):
        """Paged song list. A request with ?append=1 gets only the next rows, for the 'More' button."""
        has_more = len(songs) > PAGE
        songs = songs[:PAGE]
        next_url = more_url(offset + PAGE) if has_more else None
        return songs, next_url

    def offset_arg():
        try:
            return max(0, int(request.args.get("offset", 0)))
        except ValueError:
            return 0

    def maybe_append(songs, next_url):
        if request.args.get("append") == "1":
            return render_template("_song_rows.html", songs=songs, next_url=next_url)
        return None

    @bp.get("/")
    def home():
        return page(
            "home.html",
            playlists=library.playlists(),
            tags=shared(library.all_tags()),
            loved=library.count_loved(),
            recent=library.recently_played(limit=1),
            total=library.count_songs(),
        )

    @bp.get("/tags")
    def tags():
        chosen = clean_tags(request.args.getlist("t"))
        offset = offset_arg()
        songs = library.search_songs(tags=chosen, offset=offset, limit=PAGE + 1) if chosen else []
        songs, next_url = song_list(
            songs, offset, lambda o: url_for(".tags", t=chosen, offset=o, append=1)
        )
        return maybe_append(songs, next_url) or page(
            "tags.html", chosen=chosen, related=shared(library.related_tags(chosen)), songs=songs, next_url=next_url
        )

    @bp.get("/loved")
    def loved():
        return page("list.html", name="Loved", songs=library.loved_songs(),
                    empty="Tap the heart while a song plays to add it here.")

    @bp.get("/recent")
    def recent():
        return page("list.html", name="Played lately", songs=library.recently_played(limit=60),
                    empty="Nothing played yet.")

    @bp.get("/songs")
    def all_songs():
        order = request.args.get("order", "title")
        if order not in ("title", "added"):
            order = "title"
        offset = offset_arg()
        songs = library.search_songs(order=order, offset=offset, limit=PAGE + 1)
        songs, next_url = song_list(
            songs, offset, lambda o: url_for(".all_songs", order=order, offset=o, append=1)
        )
        return maybe_append(songs, next_url) or page("songs.html", songs=songs, next_url=next_url, order=order)

    @bp.get("/playlist/<int:playlist_id>")
    def playlist(playlist_id):
        pl = library.get_playlist(playlist_id)
        if not pl:
            abort(404)
        return page("playlist.html", playlist=pl, songs=library.playlist_songs(playlist_id))

    @bp.get("/playlists")
    def playlists():
        return page("playlists.html", playlists=library.playlists())

    return bp


# ---------------------------------------------------------------------------
# Media and the player's little API
# ---------------------------------------------------------------------------


def media_routes(library, cache, settings, tag):
    bp = Blueprint("media", __name__)

    @bp.post("/api/prepare/<song_id>")
    def prepare(song_id):
        """Ask for a song to be ready. The player polls this until it says 'ready'."""
        if not library.get_song(song_id):
            abort(404)
        prio = youtube.PREFETCH if request.args.get("prefetch") == "1" else youtube.PLAY_NOW
        return jsonify(status=cache.request(song_id, prio))

    @bp.get("/api/status/<song_id>")
    def status(song_id):
        return jsonify(status=cache.status(song_id))

    @bp.post("/api/played/<song_id>")
    def played(song_id):
        library.mark_played(song_id)
        cache.touch(song_id)
        return jsonify(ok=True)

    @bp.route("/api/love/<song_id>", methods=["GET", "POST"])
    def love(song_id):
        """GET: is the song loved? POST: love it, or stop loving it."""
        if not library.get_song(song_id):
            abort(404)
        loved = library.toggle_love(song_id) if request.method == "POST" else library.is_loved(song_id)
        return jsonify(loved=loved)

    @bp.get("/api/related/<song_id>")
    def related(song_id):
        """A few songs to play after this one when the queue has run out, from YouTube's Mix.
        New ones join the library in the "Related" playlist, with the tag "related". Skips long videos, songs played in the
        last 3 hours, and the ids in ?not= (what the player has queued already)."""
        if not settings["related"] or not youtube.VIDEO_ID.match(song_id):
            return jsonify(songs=[])
        skip = set(request.args.get("not", "").split(",")) | library.played_since(time.time() - 3 * 3600)
        try:
            found = youtube.related(song_id)
        except Exception as e:  # YouTube trouble: the player just stops, as without this feature
            log.warning("related songs for %s failed: %s", song_id, e)
            return jsonify(songs=[])
        songs = [s for s in found if s["id"] not in skip and s["duration"] and s["duration"] <= RELATED_MAX_SECONDS]
        songs = songs[:RELATED_COUNT]
        related_list = library.playlist_named(RELATED_PLAYLIST) if songs else None
        for s in songs:
            if library.add_song(s["id"], s["title"], s["channel"], s["duration"]):
                library.add_song_tags(s["id"], ["related"])
                library.add_to_playlist(related_list, s["id"])  # every song belongs to a playlist
                tag(s["id"])
        return jsonify(songs=songs)

    @bp.post("/api/log")
    def player_log():
        """What the player's video element reports (see report() in player.js), with the debug log on."""
        if settings["debug_log"]:
            log.info("player: %s", " ".join(f"{k}={v[:40]}" for k, v in request.args.items()))
        return "", 204

    @bp.get("/media/<song_id>/<name>")
    def media(song_id, name):
        """A song's HLS playlist or one of its chunks."""
        if not youtube.VIDEO_ID.match(song_id) or not (name == youtube.PLAYLIST or CHUNK.match(name)):
            abort(404)
        p = cache.song_dir(song_id) / name
        if not p.exists():
            abort(404)
        if name == youtube.PLAYLIST:
            # The playlist grows while the song is prepared; the player must always fetch it fresh.
            # A growing playlist looks like live TV, and players then start near its newest part.
            # EXT-X-START tells them to start at the beginning instead.
            text = p.read_text()
            if youtube.ENDLIST not in text:
                text = text.replace("#EXTM3U\n", "#EXTM3U\n#EXT-X-START:TIME-OFFSET=0\n", 1)
            resp = Response(text, mimetype="application/vnd.apple.mpegurl")
            resp.headers["Cache-Control"] = "no-store"
            return resp
        # conditional=True answers byte-range requests.
        return send_file(p, mimetype="video/mp2t", conditional=True)

    @bp.get("/thumb/<song_id>.jpg")
    def thumb(song_id):
        p = cache.thumbnail(song_id)
        if not p:
            abort(404)
        resp = send_file(p, mimetype="image/jpeg")
        resp.cache_control.max_age = 30 * 24 * 3600
        resp.cache_control.public = True
        return resp

    return bp


# ---------------------------------------------------------------------------
# Admin (behind a PIN)
# ---------------------------------------------------------------------------


def admin_pages(library, cache, settings, tag):
    bp = Blueprint("admin", __name__, url_prefix="/admin")

    def need_pin(view):
        @wraps(view)
        def wrapper(*a, **kw):
            if not session.get("admin"):
                return redirect(url_for(".login", next=request.full_path))
            return view(*a, **kw)

        return wrapper

    def back(default):
        target = request.form.get("back") or ""
        return redirect(target if target.startswith("/admin") else default)

    @bp.route("/login", methods=["GET", "POST"])
    def login():
        """The PIN page. The player's number pad posts here too, with ?json=1, and gets {ok}."""
        error = None
        if request.method == "POST":
            if secrets.compare_digest(request.form.get("pin", ""), str(settings["pin"])):
                session.permanent = True
                session["admin"] = True
                if request.args.get("json") == "1":
                    return jsonify(ok=True)
                nxt = request.args.get("next", "")
                return redirect(nxt if nxt.startswith("/admin") else url_for(".index"))
            time.sleep(WRONG_PIN_DELAY)  # so a short PIN cannot be found by tapping through them all
            if request.args.get("json") == "1":
                return jsonify(ok=False)
            error = "Wrong PIN"
        return render_template("admin/login.html", error=error)

    @bp.get("/session")
    def session_state():
        """Is this browser logged in to the admin? The player asks before showing its number pad."""
        return jsonify(admin=bool(session.get("admin")))

    @bp.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("player.home"))

    @bp.get("/")
    @need_pin
    def index():
        count, used = cache.usage()
        jobs = cache.pending()
        return render_template(
            "admin/index.html",
            songs=library.count_songs(),
            playlists=library.playlists(),
            cached=count,
            used=used,
            cap=settings["cache_gb"] * 1e9,
            jobs=jobs,
            default_pin=str(settings["pin"]) == Settings.DEFAULTS["pin"],
            ytdlp=youtube.version(),
        )

    @bp.get("/youtube")
    @need_pin
    def youtube_search():
        """YouTube results, YT_PAGE at a time, without the songs already in the library.
        With ?append=1 (the "More results" button) only the next rows come back, as JSON, so the
        page keeps what was ticked."""
        q = request.args.get("q", "").strip()
        offset = request.args.get("offset", 0, type=int)
        offset = max(0, offset or 0)
        found, error = [], None
        if q:
            try:
                found = youtube.search(q, YT_PAGE, offset)
            except Exception as e:
                error = str(e)
        results = [r for r in found if not library.get_song(r["id"])]
        hidden = len(found) - len(results)
        next_offset = offset + YT_PAGE if len(found) == YT_PAGE else None
        if request.args.get("append") == "1":
            if error:
                return jsonify(error=error), 502
            return jsonify(rows=render_template("admin/_yt_rows.html", results=results),
                           next=next_offset, hidden=hidden)
        return render_template(
            "admin/youtube.html", q=q, results=results, hidden=hidden, next_offset=next_offset,
            error=error, playlists=library.playlists(),
        )

    def add_songs(items, tags, playlist_id, new_playlist):
        """Add songs to the library and to a playlist. Every song must be in a playlist, so the
        caller checks with chosen_playlist() first."""
        if new_playlist.strip():
            playlist_id = library.create_playlist(new_playlist)
        added = 0
        for it in items:
            if library.add_song(it["id"], it["title"], it.get("channel", ""), it.get("duration")):
                added += 1
                tag(it["id"])
            if tags:
                library.add_song_tags(it["id"], tags)
            library.add_to_playlist(int(playlist_id), it["id"])
        return added, playlist_id

    def chosen_playlist():
        """True if the add form names an existing playlist or a new one."""
        pid = request.form.get("playlist", "")
        return bool(request.form.get("new_playlist", "").strip()) or (pid.isdigit() and library.get_playlist(int(pid)))

    def forget(song_ids):
        """Songs that left the library: their downloaded files go too."""
        for song_id in song_ids:
            cache.delete(song_id)

    @bp.post("/add")
    @need_pin
    def add():
        """Add the songs ticked on the YouTube search page."""
        items = []
        for sid in request.form.getlist("id"):
            if not youtube.VIDEO_ID.match(sid):
                continue
            dur = request.form.get(f"duration_{sid}") or None
            items.append(
                {
                    "id": sid,
                    "title": request.form.get(f"title_{sid}", sid),
                    "channel": request.form.get(f"channel_{sid}", ""),
                    "duration": int(dur) if dur and dur.isdigit() else None,
                }
            )
        if items and not chosen_playlist():
            flash("Pick a playlist or name a new one: every song belongs to a playlist.")
            return back(url_for(".index"))
        added, _ = add_songs(
            items, clean_tags(request.form.get("tags", "")), request.form.get("playlist"), request.form.get("new_playlist", "")
        )
        flash(f"Added {added} new song(s); updated {len(items) - added}." if items else "Nothing ticked.")
        return back(url_for(".index"))

    @bp.post("/add-link")
    @need_pin
    def add_link():
        url = request.form.get("url", "").strip()
        if url and not chosen_playlist():
            flash("Pick a playlist or name a new one: every song belongs to a playlist.")
            return redirect(url_for(".index"))
        try:
            items = youtube.lookup(url) if url else []
        except Exception as e:
            flash(f"Could not read that link: {e}")
            return redirect(url_for(".index"))
        added, _ = add_songs(
            items, clean_tags(request.form.get("tags", "")), request.form.get("playlist"), request.form.get("new_playlist", "")
        )
        flash(f"Found {len(items)} song(s); {added} new." if items else "No songs found at that link.")
        return redirect(url_for(".index"))

    @bp.get("/library")
    @need_pin
    def songs():
        q = request.args.get("q", "").strip()
        offset = max(0, request.args.get("offset", 0, type=int))
        rows =library.search_songs(query=q, offset=offset, limit=PAGE + 1, order="added" if not q else "title")
        return render_template(
            "admin/library.html", q=q, songs=rows[:PAGE], offset=offset, has_more=len(rows) > PAGE, page=PAGE,
            status={s["id"]: cache.status(s["id"]) for s in rows[:PAGE]},
        )

    @bp.route("/song/<song_id>", methods=["GET", "POST"])
    @need_pin
    def song(song_id):
        s = library.get_song(song_id)
        if not s:
            abort(404)
        if request.method == "POST":
            action = request.form.get("action")
            if action == "save":
                library.update_song(song_id, request.form.get("title", s["title"]).strip() or s["title"],
                                    request.form.get("channel", "").strip())
                library.set_song_tags(song_id, request.form.get("tags", ""))
                flash("Saved.")
            elif action == "add_to_playlist":
                pid = request.form.get("playlist", "")
                pid = int(pid) if pid.isdigit() and library.get_playlist(int(pid)) else None
                if request.form.get("new_playlist", "").strip():
                    pid = library.create_playlist(request.form["new_playlist"])
                if pid:
                    library.add_to_playlist(pid, song_id)
                    flash("Added to playlist.")
            elif action == "download":
                cache.request(song_id, youtube.PREFETCH)  # never interrupts what the kids are playing
                flash("Download started.")
            elif action == "drop_file":
                cache.delete(song_id)
                flash("Downloaded file removed. It will download again when played.")
            elif action == "delete":
                library.delete_song(song_id)
                cache.delete(song_id)
                flash(f"Deleted “{s['title']}”.")
                return redirect(url_for(".songs"))
            return redirect(url_for(".song", song_id=song_id))
        return render_template(
            "admin/song.html", song=s, in_playlists=library.playlists_for_song(song_id),
            playlists=library.playlists(), status=cache.status(song_id),
        )

    @bp.route("/playlists", methods=["GET", "POST"])
    @need_pin
    def playlists():
        if request.method == "POST" and request.form.get("name", "").strip():
            pid = library.create_playlist(request.form["name"])
            return redirect(url_for(".playlist", playlist_id=pid))
        return render_template("admin/playlists.html", playlists=library.playlists())

    @bp.route("/playlist/<int:playlist_id>", methods=["GET", "POST"])
    @need_pin
    def playlist(playlist_id):
        pl = library.get_playlist(playlist_id)
        if not pl:
            abort(404)
        if request.method == "POST":
            action = request.form.get("action")
            item = request.form.get("item", type=int)
            if action == "rename" and request.form.get("name", "").strip():
                library.rename_playlist(playlist_id, request.form["name"])
            elif action == "up":
                library.move_in_playlist(playlist_id, item, -1)
            elif action == "down":
                library.move_in_playlist(playlist_id, item, 1)
            elif action == "remove":
                gone = library.remove_from_playlist(playlist_id, item)
                forget(gone)
                if gone:
                    flash("That was its last playlist, so the song left the library.")
            elif action == "delete":
                gone = library.delete_playlist(playlist_id)
                forget(gone)
                flash(f"Deleted playlist “{pl['name']}”"
                      + (f" and {len(gone)} song(s) that were only in it." if gone else "."))
                return redirect(url_for(".playlists"))
            return redirect(url_for(".playlist", playlist_id=playlist_id) + (f"#i{item}" if item else ""))
        return render_template("admin/playlist.html", playlist=pl, songs=library.playlist_songs(playlist_id))

    @bp.route("/tags", methods=["GET", "POST"])
    @need_pin
    def tags():
        if request.method == "POST":
            old = request.form.get("old", "")
            if request.form.get("action") == "delete":
                library.delete_tag(old)
                flash(f"Deleted tag “{old}”.")
            elif request.form.get("new", "").strip():
                library.rename_tag(old, request.form["new"])
                flash("Tag renamed.")
            return redirect(url_for(".tags"))
        return render_template("admin/tags.html", tags=library.all_tags())

    @bp.route("/settings", methods=["GET", "POST"])
    @need_pin
    def settings_page():
        if request.method == "POST":
            pin = request.form.get("pin", "").strip()
            if pin:
                if not (pin.isdigit() and 4 <= len(pin) <= 12):  # the player's PIN pad has digits only
                    flash("The PIN must be 4 to 12 digits.")
                    return redirect(url_for(".settings_page"))
                settings["pin"] = pin
            gb = request.form.get("cache_gb", type=float)
            if gb and gb > 0:
                settings["cache_gb"] = gb
                cache.max_bytes = int(gb * 1e9)
            days = request.form.get("keep_days", type=float)
            if days and days > 0:
                settings["keep_days"] = days
                cache.keep_days = days
            settings["debug_log"] = "debug_log" in request.form
            settings["related"] = "related" in request.form
            height = request.form.get("video_height", type=int)
            if height in youtube.HEIGHTS:
                settings["video_height"] = height
                cache.max_height = height
            flash("Settings saved.")
            return redirect(url_for(".settings_page"))
        return render_template("admin/settings.html", cache_gb=settings["cache_gb"],
                               keep_days=settings["keep_days"], related=settings["related"],
                               video_height=settings["video_height"], heights=youtube.HEIGHTS)

    return bp
