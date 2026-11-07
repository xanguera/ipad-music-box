# Start playing before the song is fully prepared (HLS)

## TL;DR

Today the Mac downloads a song, converts all of it for the iPad, and only then lets the iPad play it. A 4-minute song waits about 20 seconds; a long concert waits minutes.

Instead, the Mac will serve every song as HLS (HTTP Live Streaming): Apple's own format, which Safari on iOS 9 plays natively. The converter writes the song as short chunks (about 4 seconds each) plus a playlist file that grows as chunks are added. The iPad starts as soon as the first two chunks exist. The download step stays as it is (it is fast: about 5 seconds for a song).

Expected wait for a new song: about 5 to 8 seconds. A long concert: about the time it takes to download it (roughly a minute), no longer the conversion on top.

## Decisions

- [x] Keep yt-dlp's download as is. Feeding the converter straight from YouTube would save a few seconds but YouTube slows plain requests, which is why yt-dlp downloads in pieces.
- [x] HLS with MPEG-TS chunks, H.264 + AAC: the classic form iOS 9 supports best.
- [x] Video that is already Main profile, level 3.1 or lower, is copied into chunks without re-encoding (faster, no quality loss). Other video is converted with the Mac's hardware encoder to Main 3.1, as today.
- [x] Sound is copied as is (it is already AAC).
- [x] A song is "ready" when its playlist has the end marker (`#EXT-X-ENDLIST`). It is "streaming" (playable) while being converted once at least 2 chunks exist.
- [x] Outside Safari the player uses hls.js (a small library, kept in the repo, not loaded from the internet). Chrome now claims to play HLS itself, but its player fails on a song that is still growing (see Review). The iPad and Safari never load hls.js.
- [x] Old MP4 files in the cache are deleted at startup; the cache is disposable.

## Storage

`data/media/<video id>/index.m3u8` plus `seg_00000.ts`, `seg_00001.ts`, ... Folders without the end marker at startup are leftovers of a crash and are removed. The size cap counts whole folders; the least recently played folder goes first.

## Server

- `GET /media/<id>/index.m3u8` (type `application/vnd.apple.mpegurl`, never cached, since it grows) and `GET /media/<id>/seg_NNNNN.ts` (type `video/mp2t`, byte ranges).
- `/api/status/<id>` can now also answer `streaming`.
- Skipping still cancels an unwanted download or conversion; its partial folder is deleted.

## Player

- Waits for `ready` or `streaming`, then plays `/media/<id>/index.m3u8`.
- While streaming, the length is unknown, so it shows the song's length from the library instead.

## Steps

- [x] Cache: folder layout, status with `streaming`, size cap over folders, cleanup at startup.
- [x] Converter: HLS output, copy or hardware-convert video, cancellable.
- [x] Server routes for playlist and chunks.
- [x] Player: HLS source, hls.js fallback for desktop browsers, length display.
- [x] Tests: conversion (copy and convert paths), streaming status, chunk serving.
- [x] End to end with real YouTube: time to first sound for a song and for a long concert.
- [x] Check in Chrome (hls.js) that playback, skip and auto-next still work.
- [ ] To test on the iPad: playback starts while streaming, auto-next, seeking within the converted part.

## Risks

- Seeking past the part already converted is not possible until it is converted. At 15 to 20 times real time this lasts seconds for a song.
- iOS 9 with a growing playlist: Safari treats it as a recording in progress and keeps reloading the playlist. This is standard HLS ("EVENT" playlist), but has to be confirmed on the iPad.

## Review

Built and verified on the Mac on 2026-11-05. The iPad checks above are still open.

Measured with real YouTube:
- A 4-minute song (converted from High profile): playable after 4 s, fully ready after 15 s. Before: 20 s.
- A 71-minute concert: playable after 44 s (almost all download). Before: several minutes.
- A 68-minute piece already in Main profile: copied into chunks in about 1 s after a 34 s download.
- Chunks are H.264 Main 3.1 with AAC LC at 720p.

In Chrome (through hls.js): a converted concert played for a minute while still being prepared, with no errors; skip, the copy path, the shown length and auto-next all work.

Problems found and fixed on the way:
- Copied video lacked the decoder header (SPS/PPS) in about 1 chunk in 5. YouTube uses "open GOP" key frames, where ffmpeg does not repeat the header by itself, so a player could not decode those chunks. Fixed with ffmpeg's `dump_extra` filter. A test with an open-GOP clip fails without the fix and passes with it.
- Chrome's own HLS player fails at about 12 s on any playlist that is still growing, even for a song that plays fine once finished. hls.js plays the same stream cleanly, so the stream is valid. Outside Safari the player now always uses hls.js.
- A playlist that is still growing looks like live TV, so players started near its newest part (one piece began at 48 minutes). Fixed three ways: the server adds `#EXT-X-START:TIME-OFFSET=0` to growing playlists, hls.js starts at 0, and the player jumps back to 0 if a new song starts more than 5 s in.

29 tests pass.

Not verified: Safari's HLS player (desktop or iPad) with a growing playlist. EXT-X-START support on iOS 9 is expected but not confirmed; the player's jump-back covers it if not.

Follow-up on 2026-11-07: video quality is now a setting (360p, 480p, 720p), 480p by default, since the iPad screen is 1024 x 768. YouTube often has no 480p in H.264, so the best version up to 720p is downloaded and shrunk while converting. The converted bitrate never exceeds the source's. Measured at 480p: a 3-minute song playable after 4 s, 15 MB; an 85-minute concert playable after 48 s, ready after 201 s. 34 tests pass.

Follow-up later on 2026-11-07:
- YouTube's own 480p is now downloaded when it exists (720p shrunk otherwise). The 102-minute concert became playable after 56 s instead of 67 s; the download from YouTube is still the wait.
- Songs not played for 7 days are removed (setting), checked after every preparation and hourly when idle. The 20 GB cap stays as a backstop.
- Tried: ffmpeg reading YouTube directly while converting. It ran at only 1.8 times playback speed, so it is not used. 36 tests pass.
