import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from musicbox import youtube

A, B, C = "aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"


def slow_prepare(self, song_id):
    """Pretend to prepare for up to 2 seconds, checking for cancellation like the real steps do."""
    for _ in range(40):
        self._check_cancel({})
        time.sleep(0.05)
    write_playlist(self.song_dir(song_id), chunks=1, done=True)


def write_playlist(d, chunks, done):
    d.mkdir(parents=True, exist_ok=True)
    lines = ["#EXTM3U"]
    for i in range(chunks):
        (d / f"seg_{i:05d}.ts").write_bytes(b"x")
        lines += ["#EXTINF:4.0,", f"seg_{i:05d}.ts"]
    if done:
        lines.append("#EXT-X-ENDLIST")
    (d / "index.m3u8").write_text("\n".join(lines) + "\n")


def wait_for(cache, song_id, state, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if cache.status(song_id) == state:
            return True
        time.sleep(0.02)
    return False


def new_cache(tmp_path):
    return youtube.MediaCache(tmp_path / "m", tmp_path / "t", 10**9)


def test_skipping_cancels_the_unwanted_download(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube.MediaCache, "_prepare", slow_prepare)
    cache = new_cache(tmp_path)
    cache.request(A)
    assert wait_for(cache, A, "downloading")
    started = time.time()
    cache.request(B)  # the player skipped to B
    assert wait_for(cache, B, "ready")
    assert time.time() - started < 3.5  # did not wait for A's full 2 seconds first
    assert cache.status(A) == "missing"  # A was dropped, and will be prepared again if played


def test_prefetch_does_not_cancel(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube.MediaCache, "_prepare", slow_prepare)
    cache = new_cache(tmp_path)
    cache.request(A)
    assert wait_for(cache, A, "downloading")
    cache.request(C, youtube.PREFETCH)
    assert wait_for(cache, A, "ready")
    assert wait_for(cache, C, "ready")


def test_streaming_once_two_chunks_exist(tmp_path):
    cache = new_cache(tmp_path)
    cache.states[A] = "converting"
    write_playlist(cache.song_dir(A), chunks=1, done=False)
    assert cache.status(A) == "converting"
    write_playlist(cache.song_dir(A), chunks=2, done=False)
    assert cache.status(A) == "streaming"
    write_playlist(cache.song_dir(A), chunks=3, done=True)
    assert cache.status(A) == "ready"


def test_unfinished_songs_and_old_files_are_removed_at_start(tmp_path):
    media = tmp_path / "m"
    write_playlist(media / A, chunks=2, done=False)
    write_playlist(media / B, chunks=2, done=True)
    (media / f"{C}.mp4").write_bytes(b"old")
    cache = new_cache(tmp_path)
    assert cache.status(A) == "missing" and not (media / A).exists()
    assert cache.status(B) == "ready"
    assert not (media / f"{C}.mp4").exists()


def test_size_cap_removes_least_recently_played(tmp_path):
    cache = new_cache(tmp_path)
    for sid in (A, B, C):
        write_playlist(cache.song_dir(sid), chunks=1, done=True)
        (cache.song_dir(sid) / "seg_00000.ts").write_bytes(b"x" * 1000)
        time.sleep(0.01)
    cache.touch(A)  # A played most recently
    cache.max_bytes = 2500
    cache._clean_up(keep=C)
    assert cache.status(B) == "missing" and cache.status(A) == "ready" and cache.status(C) == "ready"


def make_clip(path, profile, open_gop=False):
    """A 10-second clip with a key frame every 2 seconds. Open GOP key frames, as YouTube uses,
    are not IDR frames, so ffmpeg does not repeat the decoder header at them by itself."""
    x264 = ["-x264-params", "open-gop=1:keyint=50:min-keyint=50:scenecut=0"] if open_gop else ["-g", "50"]
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=1280x720:r=25:d=10", "-f", "lavfi",
         "-i", "anullsrc=r=44100", "-t", "10", "-c:v", "libx264", *x264, "-profile:v", profile,
         "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)],
        check=True,
    )


def check_chunks(cache, song_id, width):
    assert cache.is_complete(song_id)
    chunks = sorted(cache.song_dir(song_id).glob("seg_*.ts"))
    assert len(chunks) >= 2  # 10 seconds in chunks of about 4
    profile, level, w, _ = youtube._video_profile(chunks[0])
    assert profile in youtube.IPAD_PROFILES and level <= 31 and w == width
    for chunk in chunks:  # each chunk must decode on its own, as a player that jumps in reads it
        out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(chunk), "-f", "null", "-"],
                             capture_output=True, text=True).stderr
        assert "PPS" not in out and "no frame" not in out, (chunk.name, out[:200])


def test_high_profile_video_is_converted_to_main(tmp_path):
    src = tmp_path / "high.mp4"
    make_clip(src, "high")
    assert youtube._video_profile(src)[0] == "High"
    cache = new_cache(tmp_path)
    cache.max_height = 720
    cache._write_hls(A, src)
    check_chunks(cache, A, 1280)


def test_main_profile_video_is_copied(tmp_path):
    src = tmp_path / "main.mp4"
    make_clip(src, "main")
    cache = new_cache(tmp_path)
    cache.max_height = 720
    cache._write_hls(A, src)
    check_chunks(cache, A, 1280)


def test_video_wider_than_the_chosen_quality_is_shrunk(tmp_path):
    src = tmp_path / "main.mp4"
    make_clip(src, "main")  # 1280 x 720, Main profile: would be copied at 720p
    cache = new_cache(tmp_path)  # default quality, 480p
    cache._write_hls(A, src)
    check_chunks(cache, A, 854)


def test_open_gop_copy_repeats_decoder_header_in_every_chunk(tmp_path):
    src = tmp_path / "opengop.mp4"
    make_clip(src, "main", open_gop=True)
    cache = new_cache(tmp_path)
    cache.max_height = 720  # so the clip is copied, not converted
    cache._write_hls(A, src)
    check_chunks(cache, A, 1280)


@pytest.mark.parametrize("height, source_kbit, expected", [
    (480, 5000, "1000k"),  # a rich source: the quality's own bitrate
    (480, 476, "476k"),  # a thin source: no more bits than it has
    (720, 100, "300k"),  # never starved below a floor
])
def test_converted_bitrate(height, source_kbit, expected):
    assert youtube.encoders(height, source_kbit)[0][2] == expected


def test_download_prefers_the_chosen_quality():
    """With formats like YouTube's, yt-dlp picks the chosen height, or 720p when it is missing."""
    import yt_dlp
    def fmt(fid, h):
        return {"format_id": fid, "height": h, "width": h * 16 // 9, "fps": 25, "vcodec": "avc1.4d401f",
                "acodec": "none", "ext": "mp4", "url": "x", "protocol": "https"}
    audio = {"format_id": "140", "vcodec": "none", "acodec": "mp4a.40.2", "ext": "m4a", "url": "x",
             "protocol": "https", "abr": 128}
    with_480 = [fmt("134", 360), fmt("135", 480), fmt("136", 720), audio]
    without_480 = [fmt("134", 360), fmt("136", 720), audio]
    ydl = yt_dlp.YoutubeDL({"quiet": True})
    def pick(formats, height):
        select = ydl.build_format_selector(youtube.video_format(height))
        return next(select({"formats": formats, "has_merged_format": False, "incomplete_formats": False}))["format_id"]
    assert pick(with_480, 480) == "135+140"
    assert pick(without_480, 480) == "136+140"
    assert pick(with_480, 720) == "136+140"
    assert pick(with_480, 360) == "134+140"


def test_songs_not_played_for_days_are_removed(tmp_path):
    cache = new_cache(tmp_path)
    for song_id, days_ago in [(A, 8), (B, 6), (C, 30)]:
        d = cache.song_dir(song_id)
        d.mkdir(parents=True)
        (d / youtube.PLAYLIST).write_text(youtube.ENDLIST)
        then = time.time() - days_ago * 86400
        os.utime(d, (then, then))
    cache._clean_up(keep=C)  # C is the song just prepared: kept whatever its age
    assert [cache.song_dir(s).exists() for s in (A, B, C)] == [False, True, True]


class GrowingFile(youtube.Download):
    """Stands in for a yt-dlp download: writes NAME.part a piece at a time, then renames it."""

    def __init__(self, src, stem, fail_halfway=False):
        self.final = Path(f"{stem}.mp4")
        self.error = None
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._write, args=(src.read_bytes(), fail_halfway), daemon=True)
        self.thread.start()

    def _write(self, data, fail_halfway):
        part, step = Path(f"{self.final}.part"), len(data) // 20 + 1
        with open(part, "wb") as f:
            for i in range(0, len(data), step):
                if fail_halfway and i > len(data) // 2:
                    self.error = RuntimeError("network gone")
                    self.done.set()
                    return
                f.write(data[i:i + step])
                f.flush()
                time.sleep(0.1)
        part.rename(self.final)
        self.done.set()


def make_dash_pair(tmp_path, seconds=20):
    """Picture and sound as separate files, fragmented like YouTube's."""
    video, audio = tmp_path / "v.mp4", tmp_path / "a.m4a"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"testsrc=s=854x480:r=25:d={seconds}",
                    "-c:v", "libx264", "-profile:v", "main", "-g", "50", "-pix_fmt", "yuv420p",
                    "-movflags", "frag_keyframe+empty_moov+default_base_moof", str(video)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=d={seconds}", "-c:a", "aac",
                    "-movflags", "frag_keyframe+empty_moov+default_base_moof", str(audio)], check=True)
    return video, audio


def test_chunks_are_made_while_the_picture_is_still_downloading(tmp_path):
    video, audio = make_dash_pair(tmp_path)
    cache = new_cache(tmp_path)
    growing = GrowingFile(video, tmp_path / "dl")
    cache.states[A] = "converting"  # as _prepare marks it
    writer = threading.Thread(target=cache._write_hls, args=(A, growing),
                              kwargs={"audio": audio, "probe": ("Main", 30, 854, 1000)})
    writer.start()
    while cache.status(A) != "streaming" and writer.is_alive():
        time.sleep(0.05)
    assert cache.status(A) == "streaming" and not growing.done.is_set()  # playable before the download ends
    writer.join()
    check_chunks(cache, A, 854)
    playlist = cache.playlist(A).read_text()
    total = sum(float(x.split(",")[0]) for x in playlist.split("#EXTINF:")[1:])
    assert abs(total - 20) < 0.5  # the whole song, not just what had arrived at the start
    streams = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0",
                              str(cache.song_dir(A) / "seg_00000.ts")], capture_output=True, text=True).stdout
    assert "audio" in streams


def test_a_download_failing_halfway_leaves_no_finished_song(tmp_path):
    video, audio = make_dash_pair(tmp_path)
    cache = new_cache(tmp_path)
    growing = GrowingFile(video, tmp_path / "dl", fail_halfway=True)
    with pytest.raises(RuntimeError, match="network gone"):
        cache._write_hls(A, growing, audio=audio, probe=("Main", 30, 854, 1000))
    assert not cache.is_complete(A)


@pytest.mark.parametrize("vcodec, expected", [
    ("avc1.4D401E", ("Main", 30)),
    ("avc1.64001F", ("High", 31)),
    ("avc1.42001E", ("Baseline", 30)),
    ("vp09.00.40.08", ("", 99)),
])
def test_profile_from_youtube_codec_string(vcodec, expected):
    assert youtube._codec_profile({"vcodec": vcodec, "width": 854, "vbr": 500})[:2] == expected


def test_failed_thumbnail_is_not_fetched_again_at_once(tmp_path, monkeypatch):
    calls = []

    def offline(url, timeout):
        calls.append(url)
        raise OSError("offline")

    monkeypatch.setattr(youtube.urllib.request, "urlopen", offline)
    cache = new_cache(tmp_path)
    assert cache.thumbnail(A) is None
    assert cache.thumbnail(A) is None  # answered at once, without a second 10-second wait
    assert len(calls) == 1
