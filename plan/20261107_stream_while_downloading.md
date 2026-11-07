# Make chunks while the video is still downloading

## TL;DR

A new song only starts after the whole video has come down from YouTube: picture first, then sound, then the two are joined. A 102-minute concert waits 56 s.

Instead, the Mac downloads picture and sound at the same time. The sound is small and arrives in a few seconds. As soon as it has, ffmpeg starts making chunks from the picture file while that file is still downloading. When ffmpeg catches up with the download, it waits for more data instead of ending the song. Expected: a long concert plays after about 15 s, a normal song a little sooner than today's 4 s.

Tested beforehand (2026-11-07): the 102-minute concert's sound file (99 MB) arrived in under 12 s; a picture file still downloading was readable up to the part already downloaded (63 MB gave 20 minutes).

## Design

- [x] Ask YouTube once for the video's details. Pick the formats with the same rules as today, then download the chosen picture and sound formats in two threads. Only plain https formats, which yt-dlp writes from start to end in order (not YouTube's HLS formats, which arrive in pieces).
- [x] When YouTube offers only a file with both (format 18), it is downloaded completely first, as today (see Review).
- [x] Wait for the sound to finish. Then start ffmpeg with the sound file and a pipe for the picture. A small "follower" thread reads the growing picture file and passes it into the pipe. When it reaches the end of what is downloaded, it waits and tries again, until the download has finished. Then it closes the pipe, ffmpeg finishes, and the playlist gets its end marker.
- [x] yt-dlp writes `x.mp4.part` and renames it at the end. The follower keeps its open file, which survives the rename.
- [x] Copy or convert is decided from YouTube's format details (codec string such as `avc1.4D401E` gives profile and level; width and bitrate are listed), since ffprobe cannot read a file that is not there yet.
- [x] If the picture download fails halfway, the follower kills ffmpeg (so the song never gets an end marker) and the song is removed, as any failed song is today.
- [x] Skipping cancels both downloads and ffmpeg, as today.
- [x] If the hardware encoder fails and the software one is tried, the follower starts again from the beginning of the file.

## Steps

- [x] Plan (this file).
- [x] Code in `musicbox/youtube.py`.
- [x] Tests: chunks appear while a file is still being written, and the result is complete; a download that fails halfway leaves no song; profile read from the codec string.
- [x] End to end with real YouTube: time to play for a song and the concert; the finished concert has its full length; skipping during a download stops both downloads.
- [x] README, review.

## Review

Built and verified on 2026-11-07. 42 tests pass.

Measured with real YouTube, at 480p:
- The 102-minute concert: playable after 14 s (was 56 s), fully ready after 35 s. All 1531 chunks, the full 102 minutes, with sound and picture to the last chunk.
- A 3-minute song: playable after 3 s (was 4 s).
- Some videos make yt-dlp wait about 4 s before their files can be fetched ("Sleeping 4.00 seconds as required by the site"). Such songs start after about 8 s. This wait was there before too and cannot be avoided.
- Skipping in the middle of a long download stopped both downloads and ffmpeg within a second and removed the partial files.

Tests added: chunks become playable while a file is still being written and the result covers the whole clip; a download failing halfway leaves no finished song; profile and level read from YouTube's codec string.

Choices made on the way:
- A single file with both picture and sound (format 18, the last resort) is still downloaded completely first, because such a file may keep its index at its end.
- yt-dlp's container fix-up is switched off for these downloads, since it rewrites a file that ffmpeg may be reading. ffmpeg reads YouTube's files without it.
