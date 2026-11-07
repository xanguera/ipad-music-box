"""Everything that talks to YouTube, through yt-dlp.

Search and link lookup only read metadata. Songs are prepared one at a time in a background
thread, the first time a song is played (or just before, as a prefetch of the next song).
"""

import copy
import logging
import os
import re
import json
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from queue import Empty, PriorityQueue

import yt_dlp

log = logging.getLogger(__name__)

# The iPad 2 plays H.264 video up to 720p at 30 frames per second (Main profile, level 3.1),
# with AAC sound, in MP4. So take H.264 video at the chosen quality, plus YouTube's 128 kbit/s AAC.
# YouTube often has no H.264 at 480p; then take the best up to 720p, which is shrunk after the
# download. Format 18 (360p in one file, low bitrate) is only the last resort.
# YouTube often offers video only in High profile, which the iPad 2 cannot play. Such files are
# converted to Main profile after the download (see MediaCache._write_hls).
# Only plain https formats: yt-dlp writes them from start to end in order, so the picture can be
# read while it downloads. (YouTube's HLS formats arrive in pieces.)
H264 = "[fps<=30][vcodec^=avc1][protocol=https]"
AAC = "[ext=m4a][protocol=https]"


def video_format(height):
    """yt-dlp format string: H.264 at the chosen height if YouTube has it, else the best up to 720p."""
    lower = {480: 360, 720: 480}.get(height, 240)
    return (
        f"bv*[height<={height}][height>{lower}]{H264}+ba{AAC}"
        f"/bv*[height<=720]{H264}+ba{AAC}"
        f"/b[height<=720]{H264}[ext=mp4]"
        "/18"
    )


HEIGHTS = (360, 480, 720)  # the choices in Settings
DEFAULT_HEIGHT = 480  # the iPad 2 screen is 1024 x 768, so 720p shows little more detail
WIDTHS = {360: 640, 480: 854, 720: 1280}  # by width, so wide-screen videos keep their shape


def encoders(height, source_kbit):
    """ffmpeg video encoders to try, hardware first. The bitrate follows the height, but never
    exceeds the source's: YouTube's own bitrate is often low, and more bits would not look better."""
    kbit = min({360: 700, 480: 1000}.get(height, 1500), max(source_kbit, 300))
    limits = ["-maxrate", f"{kbit * 4 // 3}k", "-bufsize", f"{kbit * 2}k"]
    return [
        ["h264_videotoolbox", "-b:v", f"{kbit}k", *limits],
        ["libx264", "-preset", "veryfast", "-crf", "23", *limits],
    ]


class Cancelled(Exception):
    """A download nobody wants any more, because the player moved on to another song."""

VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

PLAY_NOW, PREFETCH = 0, 1


def base_options():
    opts = {"quiet": True, "no_warnings": True, "noprogress": True}
    # yt-dlp needs a JavaScript runtime for YouTube. Deno is its default; use Node if that is what we have.
    if not shutil.which("deno") and shutil.which("node"):
        opts["js_runtimes"] = {"node": {}}
        # yt-dlp runs Node with file access switched off. A preload script in NODE_OPTIONS
        # (some terminals add one) then fails, and YouTube's challenge goes unsolved.
        os.environ.pop("NODE_OPTIONS", None)
    return opts


def search(query, limit=20, offset=0):
    """Search YouTube: results offset+1 to offset+limit, as [{id, title, channel, duration}].
    Metadata only, fast (a later page takes a little longer: YouTube is read up to it)."""
    opts = base_options() | {"extract_flat": "in_playlist", "skip_download": True,
                             "playlist_items": f"{offset + 1}:{offset + limit}"}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"ytsearch{offset + limit}:{query}", download=False)
    return [_entry(e) for e in info.get("entries") or [] if e and VIDEO_ID.match(e.get("id") or "")]


def lookup(url):
    """Resolve a YouTube video or playlist link to a list of songs (metadata only)."""
    opts = base_options() | {"extract_flat": "in_playlist", "skip_download": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = info.get("entries")
    if entries is None:
        entries = [info]
    return [_entry(e) for e in entries if e and VIDEO_ID.match(e.get("id") or "")]


def related(song_id, limit=25):
    """Songs YouTube suggests after this one: its "Mix" playlist (list=RD<id>), metadata only."""
    opts = base_options() | {"extract_flat": "in_playlist", "skip_download": True, "playlistend": limit + 1}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={song_id}&list=RD{song_id}", download=False)
    return [_entry(e) for e in info.get("entries") or []
            if e and VIDEO_ID.match(e.get("id") or "") and e["id"] != song_id]


def _entry(e):
    duration = e.get("duration")
    return {
        "id": e["id"],
        "title": e.get("title") or e["id"],
        "channel": e.get("channel") or e.get("uploader") or "",
        "duration": int(duration) if duration else None,
    }


def version():
    return yt_dlp.version.__version__


class MediaCache:
    """Songs ready for the iPad, as HLS: one folder per song with a playlist and short chunks.

    A song is prepared on demand: yt-dlp downloads it, then ffmpeg writes it as chunks. The iPad
    may start playing once the first chunks exist, while ffmpeg is still writing the rest.
    Songs not played for keep_days are removed. On top of that the folder holds at most max_bytes;
    the least recently played songs are removed first.
    """

    def __init__(self, folder: Path, thumbs: Path, max_bytes: int, max_height: int = DEFAULT_HEIGHT,
                 keep_days: float = 7):
        self.folder = folder
        self.keep_days = keep_days
        self.max_height = max_height  # applies to songs prepared from now on
        self.thumbs = thumbs
        self._thumb_failed = {}  # song id -> time its thumbnail last failed to download
        self.max_bytes = max_bytes
        folder.mkdir(parents=True, exist_ok=True)
        thumbs.mkdir(parents=True, exist_ok=True)
        self._clean_leftovers()
        self.queue = PriorityQueue()
        self.lock = threading.Lock()
        self.states = {}  # id -> "queued" | "downloading" | "converting" | "error: ..."
        self.counter = 0
        self.current = None  # the song being prepared right now
        self.cancel = threading.Event()
        self.worker = threading.Thread(target=self._run, name="downloader", daemon=True)
        self.worker.start()

    def song_dir(self, song_id):
        return self.folder / song_id

    def playlist(self, song_id):
        return self.song_dir(song_id) / PLAYLIST

    def is_complete(self, song_id):
        p = self.playlist(song_id)
        try:
            return p.exists() and ENDLIST in p.read_text()
        except OSError:
            return False

    def chunks_listed(self, song_id):
        try:
            return self.playlist(song_id).read_text().count("#EXTINF")
        except OSError:
            return 0

    def status(self, song_id):
        """ready (fully prepared), streaming (playable, still being written), queued, downloading,
        converting, missing (never asked for), or "error: ..."."""
        if self.is_complete(song_id):
            return "ready"
        with self.lock:
            state = self.states.get(song_id, "missing")
        if state == "converting" and self.chunks_listed(song_id) >= MIN_CHUNKS_TO_PLAY:
            return "streaming"
        return state

    def request(self, song_id, priority=PLAY_NOW):
        """Make sure a song gets prepared. Returns its status right now."""
        if not VIDEO_ID.match(song_id):
            return "error: bad id"
        if self.is_complete(song_id):
            return "ready"
        with self.lock:
            state = self.states.get(song_id)
            busy = state in ("downloading", "converting")
            if busy or (state == "queued" and priority == PREFETCH):
                pass
            else:
                if priority == PLAY_NOW and self.current not in (None, song_id):
                    # The player skipped to another song: stop preparing the one it no longer
                    # needs, so this one does not wait behind it (a long concert takes a minute).
                    self.cancel.set()
                # New request, a retry after an error, or a prefetched song now wanted right away.
                self.states[song_id] = "queued"
                self.counter += 1
                self.queue.put((priority, self.counter, song_id))
        return self.status(song_id)

    def touch(self, song_id):
        """Mark a song as played now. Its folder's time is what the clean-up goes by."""
        d = self.song_dir(song_id)
        if d.exists():
            os.utime(d)

    def delete(self, song_id):
        with self.lock:
            if self.current == song_id:
                self.cancel.set()
            self.states.pop(song_id, None)
        shutil.rmtree(self.song_dir(song_id), ignore_errors=True)

    def usage(self):
        """(number of prepared songs, bytes used)."""
        dirs = self._song_dirs()
        return len(dirs), sum(_dir_size(d) for d in dirs)

    def pending(self):
        with self.lock:
            return dict(self.states)

    def _song_dirs(self):
        return [d for d in self.folder.iterdir() if d.is_dir() and VIDEO_ID.match(d.name)]

    def _clean_leftovers(self):
        """Remove what a crash or an older version left behind: unfinished songs and old MP4 files."""
        for d in self._song_dirs():
            if not self.is_complete(d.name):
                shutil.rmtree(d, ignore_errors=True)
        for f in self.folder.glob("*.mp4"):
            f.unlink(missing_ok=True)
        shutil.rmtree(self.folder / "tmp", ignore_errors=True)

    def _run(self):
        while True:
            try:
                _, _, song_id = self.queue.get(timeout=3600)
            except Empty:  # an hour with nothing to prepare: a good moment to clean up
                self._clean_up(keep=None)
                continue
            with self.lock:
                if self.states.get(song_id) != "queued":
                    continue  # already handled through an earlier queue entry
                self.states[song_id] = "downloading"
                self.current = song_id
                self.cancel.clear()
            try:
                if not self.is_complete(song_id):
                    self._prepare(song_id)
                with self.lock:
                    self.states.pop(song_id, None)
                self._clean_up(keep=song_id)
            except Exception as e:  # yt-dlp raises many kinds; show the message to the admin
                shutil.rmtree(self.song_dir(song_id), ignore_errors=True)
                if self.cancel.is_set():
                    log.info("stopped preparing %s: the player moved on", song_id)
                    with self.lock:
                        self.states.pop(song_id, None)
                else:
                    log.warning("preparing %s failed: %s", song_id, e)
                    with self.lock:
                        self.states[song_id] = f"error: {_short(e)}"
            finally:
                for part in (self.folder / "tmp").glob(f"{song_id}.*"):
                    part.unlink(missing_ok=True)
                with self.lock:
                    self.current = None

    def _prepare(self, song_id):
        """Download picture and sound at the same time. The sound is small and comes first; then
        ffmpeg makes chunks from the picture while it is still downloading."""
        started = time.time()
        tmp = self.folder / "tmp"
        tmp.mkdir(exist_ok=True)
        log.info("downloading %s", song_id)
        info = self._details(song_id)
        picture, sound = self._pick(info)
        stop = threading.Event()  # set when this preparation ends, so no download outlives it

        def check(_progress):
            if stop.is_set() or self.cancel.is_set():
                raise Cancelled()

        video = Download(info, picture, tmp / f"{song_id}.v", check)
        audio = Download(info, sound, tmp / f"{song_id}.a", check) if sound else None
        try:
            if audio:
                audio.wait()
                with self.lock:
                    self.states[song_id] = "converting"
                self._write_hls(song_id, video, audio=audio.path(), probe=_codec_profile(picture))
                video.wait()
            else:
                # A single file with picture and sound (format 18) may keep its index at the end,
                # which ffmpeg cannot read before the file is complete. It is small; wait for it.
                video.wait()
                with self.lock:
                    self.states[song_id] = "converting"
                self._write_hls(song_id, video.path())
        finally:
            stop.set()
            for d in (video, audio):
                if d:
                    d.thread.join(timeout=30)
        log.info("prepared %s in %.0f s (%.1f MB)", song_id, time.time() - started,
                 _dir_size(self.song_dir(song_id)) / 1e6)

    def _details(self, song_id):
        """Ask YouTube once for the video's formats; both downloads reuse the answer."""
        with yt_dlp.YoutubeDL(base_options()) as ydl:
            return ydl.extract_info(f"https://www.youtube.com/watch?v={song_id}", download=False, process=False)

    def _pick(self, info):
        """(picture format, sound format) by the usual rules; sound is None for a file with both."""
        with yt_dlp.YoutubeDL(base_options() | {"format": video_format(self.max_height)}) as ydl:
            chosen = ydl.process_ie_result(copy.deepcopy(info), download=False)
        parts = chosen.get("requested_formats") or [chosen]
        picture = next(f for f in parts if f.get("vcodec") not in (None, "none"))
        sound = next((f for f in parts if f.get("vcodec") == "none"), None)
        return picture, sound

    def _write_hls(self, song_id, source, audio=None, probe=None):
        """Write the song as HLS chunks. The playlist grows as ffmpeg goes, so playback can start early.

        source is a finished file, or a Download still running: then its file is passed to ffmpeg
        as it grows. audio is a separate sound file; probe is (profile, level, width, kbit/s) when
        the file cannot be read yet.

        Video already in a form the iPad 2 plays (Main profile, level 3.1 or lower) and no wider than
        the chosen quality is copied as is. Anything else is converted with the Mac's hardware
        encoder, shrunk to the chosen quality if needed. Sound is copied (it is AAC).
        """
        growing = isinstance(source, Download)
        profile, level, width, kbit = probe or _video_profile(source)
        max_width = WIDTHS.get(self.max_height, 1280)
        copy = profile in IPAD_PROFILES and level <= 31 and width <= max_width
        shrink = ["-vf", f"scale={max_width}:-2"] if width > max_width else []
        tries = [["copy"]] if copy else encoders(self.max_height, kbit)
        out = self.song_dir(song_id)
        log.info("%s %s (%s %s) into chunks", "copying" if copy else "converting", song_id, profile, level)
        for encoder in tries:  # hardware encoder first, software as a fallback
            shutil.rmtree(out, ignore_errors=True)
            out.mkdir(parents=True)
            # Repeat the decoder header (SPS/PPS) at every key frame, so every chunk can be decoded
            # on its own. ffmpeg adds it only at some frame types, which YouTube files do not always use.
            video = ["-c:v", "copy", "-bsf:v", "h264_mp4toannexb,dump_extra=freq=keyframe"] if copy else [
                *shrink, "-c:v", *encoder, "-profile:v", "main", "-level", "3.1", "-pix_fmt", "yuv420p",
                # A key frame every CHUNK_SECONDS, so every chunk can start on its own.
                "-force_key_frames", f"expr:gte(t,n_forced*{CHUNK_SECONDS})",
            ]
            inputs = ["-i", "pipe:0" if growing else str(source)]
            if audio:
                inputs += ["-i", str(audio), "-map", "0:v:0", "-map", "1:a:0"]
            cmd = [
                shutil.which("ffmpeg") or "ffmpeg", "-v", "error", "-y", *inputs,
                *video, "-c:a", "copy",
                "-f", "hls", "-hls_time", str(CHUNK_SECONDS), "-hls_list_size", "0",
                "-hls_playlist_type", "event",  # a growing playlist the player reloads until it ends
                "-hls_segment_filename", str(out / "seg_%05d.ts"), str(out / PLAYLIST),
            ]
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if growing else subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            if growing:  # each try reads the file again from its start
                threading.Thread(target=_follow, args=(source, proc), name="follower", daemon=True).start()
            while proc.poll() is None:
                if self.cancel.is_set():
                    proc.kill()
                    proc.wait()
                    raise Cancelled()
                time.sleep(0.2)
            if growing and source.error:  # the follower stopped ffmpeg; the song is incomplete
                raise source.error
            if proc.returncode == 0 and self.is_complete(song_id):
                return
            log.warning("writing %s with %s failed: %s", song_id, encoder[0], proc.stderr.read().decode()[-300:])
        raise RuntimeError("could not prepare the video for the iPad")

    def _check_cancel(self, _progress):
        if self.cancel.is_set():
            raise Cancelled()

    def _clean_up(self, keep):
        """Remove songs not played for keep_days, then the least recently played ones while the
        folder is over max_bytes. Runs on the worker thread only, so never during a preparation."""
        cutoff = time.time() - self.keep_days * 86400
        for d in self._song_dirs():
            if d.name != keep and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                log.info("not played for %g days, removed %s", self.keep_days, d.name)
        dirs = sorted(self._song_dirs(), key=lambda d: d.stat().st_mtime)
        sizes = {d: _dir_size(d) for d in dirs}
        total = sum(sizes.values())
        for d in dirs:
            if total <= self.max_bytes:
                break
            if d.name == keep:
                continue
            total -= sizes[d]
            shutil.rmtree(d, ignore_errors=True)
            log.info("cache full, removed %s", d.name)

    def thumbnail(self, song_id):
        """Path to a cached 320x180 thumbnail, fetched from YouTube the first time."""
        if not VIDEO_ID.match(song_id):
            return None
        p = self.thumbs / f"{song_id}.jpg"
        if not p.exists():
            # A failed fetch is not tried again for a while: offline, every list page would
            # otherwise hold many server threads for the full timeout, and the playing song's
            # chunks would wait behind them.
            if time.time() - self._thumb_failed.get(song_id, 0) < THUMB_RETRY_SECONDS:
                return None
            try:
                with urllib.request.urlopen(f"https://i.ytimg.com/vi/{song_id}/mqdefault.jpg", timeout=10) as r:
                    data = r.read()
                tmp = p.with_suffix(f".{threading.get_ident()}.part")  # two requests may fetch at once
                tmp.write_bytes(data)
                tmp.replace(p)
            except Exception as e:
                log.warning("thumbnail of %s failed: %s", song_id, e)
                self._thumb_failed[song_id] = time.time()
                return None
        return p


class Download:
    """One yt-dlp download of a single format, running in its own thread."""

    def __init__(self, info, fmt, stem, progress_hook):
        self.final = Path(f"{stem}.{fmt['ext']}")
        self.error = None
        self.done = threading.Event()
        self.opts = base_options() | {
            "format": fmt["format_id"],
            "outtmpl": f"{stem}.%(ext)s",
            "progress_hooks": [progress_hook],
            "fixup": "never",  # a container fix rewrites the file, which the follower may be reading
        }
        self.thread = threading.Thread(target=self._run, args=(copy.deepcopy(info),),
                                       name=f"download {fmt['format_id']}", daemon=True)
        self.thread.start()

    def _run(self, info):
        try:
            with yt_dlp.YoutubeDL(self.opts) as ydl:
                ydl.process_ie_result(info, download=True)
            if not self.final.exists():
                raise RuntimeError(f"the download left no {self.final.name}")
        except BaseException as e:
            self.error = e
        finally:
            self.done.set()

    def wait(self):
        self.done.wait()
        if self.error:
            raise self.error

    def path(self):
        return self.final

    def open(self):
        """The file as it is now: yt-dlp writes NAME.part and renames it when done. An open file
        survives the rename. None if neither exists yet."""
        for p in (self.final, Path(f"{self.final}.part")):
            try:
                return open(p, "rb")
            except FileNotFoundError:
                pass
        return None


def _follow(download, proc):
    """Pass a file that is still downloading into ffmpeg. At the end of what has arrived, wait for
    more, until the download has finished. If it fails, stop ffmpeg so the song never looks done."""
    try:
        f = download.open()
        while f is None:
            if download.done.is_set():
                f = download.open()  # renamed between the two checks
                if f is None:
                    proc.kill()
                    return
                break
            if proc.poll() is not None:
                return
            time.sleep(0.1)
        with f:
            while True:
                finished = download.done.is_set()  # checked before reading, so nothing is missed
                data = f.read(1 << 20)
                if data:
                    proc.stdin.write(data)
                elif finished:
                    if download.error:
                        proc.kill()
                    return
                elif proc.poll() is not None:
                    return
                else:
                    time.sleep(0.2)
    except OSError:  # ffmpeg stopped (skip, or failed): nothing more to pass on
        pass
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass


PLAYLIST = "index.m3u8"
ENDLIST = "#EXT-X-ENDLIST"
CHUNK_SECONDS = 4
MIN_CHUNKS_TO_PLAY = 2  # enough for Safari to start without stalling at once
THUMB_RETRY_SECONDS = 600  # wait before trying a failed thumbnail again
IPAD_PROFILES = {"Main", "Baseline", "Constrained Baseline"}


def _dir_size(d):
    return sum(f.stat().st_size for f in d.iterdir() if f.is_file())


def _video_profile(path):
    """(profile, level, width, kbit/s) of the first video stream, e.g. ("High", 31, 1280, 476).
    ("", 99, 99999, 99999) if it cannot be read, so the file is converted."""
    try:
        out = subprocess.run(
            [shutil.which("ffprobe") or "ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=profile,level,width,bit_rate", "-of", "json", str(path)],
            capture_output=True, check=True, timeout=30,
        ).stdout
        stream = json.loads(out)["streams"][0]
        kbit = int(stream.get("bit_rate", 99999000)) // 1000  # MPEG-TS chunks carry no bitrate
        return stream.get("profile", ""), int(stream.get("level", 99)), int(stream.get("width", 99999)), kbit
    except Exception:
        return "", 99, 99999, 99999


def _codec_profile(fmt):
    """(profile, level, width, kbit/s) from YouTube's format details, e.g. vcodec "avc1.4D401E"
    is Main profile, level 3.0. Used when the file is not downloaded yet."""
    m = re.match(r"avc1\.([0-9A-Fa-f]{2})[0-9A-Fa-f]{2}([0-9A-Fa-f]{2})", fmt.get("vcodec") or "")
    profile = {66: "Baseline", 77: "Main", 100: "High"}.get(int(m[1], 16), "") if m else ""
    level = int(m[2], 16) if m else 99
    kbit = int(fmt.get("vbr") or fmt.get("tbr") or 99999)
    return profile, level, int(fmt.get("width") or 99999), kbit


def _short(e):
    msg = str(e).replace("ERROR: ", "").strip()
    msg = re.sub(r"\x1b\[[0-9;]*m", "", msg)  # colour codes
    return msg[:200]
