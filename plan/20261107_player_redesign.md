# Player redesign: loved songs, true full screen, lists and tags, admin button, endless play

## TL;DR

Six changes to the kids' player page:

1. A heart button marks a song as loved. Loved songs form a "Loved" list, kept on the Mac.
2. Full screen shows only the video. A tap anywhere shows the controls for a few seconds: "Small" at the top right, the heart below it, play controls along the bottom. No search there.
3. The "Music" button at the top left goes.
4. The free-text search goes. Browsing is by lists and tags only. Lists are the admin's playlists plus "Loved" and "Played lately". Tags are set automatically by the Mac for each song (artist and genre, from MusicBrainz), and the admin can still edit them.
5. The top bar gets a green "Lists & tags" button and an orange "Admin" button. Admin opens the admin pages over the player, behind the PIN, so the music keeps playing.
6. When the queue runs out, play continues with related songs from YouTube's "Mix" for the last song.

## Findings that shaped this (2026-11-07)

- YouTube Music (music.youtube.com) gives no extra data for these videos through yt-dlp: no genre, and artist, track and album are empty for normal videos. It is not a better source of tags.
- YouTube's own keywords are the uploader's choice and noisy (for Queen: "Freddie", "Mercury", "Brian", ...).
- MusicBrainz, a free open music database, has genre tags per artist: Queen gives "rock, glam rock, hard rock"; Pinkfong gives "children's music". It needs no key; the limit is one request per second.
- YouTube's Mix playlist for a video (`list=RD<id>`) lists related songs. For a Marais piece by Jordi Savall, all of the first entries were baroque viol music.

## Design

### Loved songs
- [x] Table `loved (song_id, loved_at)`. `POST /api/love/<id>` toggles and returns the new state.
- [x] Heart button in the player bar and in full screen. Filled when the song playing is loved.
- [x] "Loved" list page, newest first.

### Full screen
- [x] Full screen hides the top bar, the browse area and the player bar; the video fills the screen (black around it).
- [x] A tap shows: "Small" top right, heart below it, previous / play / next along the bottom. They hide again after 4 s without a tap.
- [x] A tap on a control works at once (no need to tap twice).

### Browsing
- [x] Top bar: green "Lists & tags" button (goes to the browse home) and orange "Admin" button. No search field, no "Music" button.
- [x] Browse home: Lists first (Loved, Played lately, then the admin's playlists, as tiles), then Tags (the tag cloud, as today: tap tags to narrow).
- [x] The search page and route go from the player. Admin search stays.

### Automatic tags
- [x] When a song is added, a background job asks MusicBrainz who the artist is and which genres the artist has: a recording search with the title, accepted only above a confidence score; then the artist's top genres. Adds the artist name and up to 3 genres as tags. One request per second, never blocking the admin page.
- [x] Existing songs get the same once (on startup, for songs never looked up). A column records that a song was looked up, so it happens once.
- [x] Wrong guesses are fixed by hand in the admin, as tags are today. A song the admin tagged keeps those tags; automatic tags are only added.

### Endless play
- [x] When the last song in the queue ends, the player asks `GET /api/related/<id>`. The server reads YouTube's Mix for that song and returns a few songs not played in the last few hours, adds them to the library with the tag "related", and the player queues and plays them.
- [x] Setting "Continue with related songs" (on by default), since YouTube picks these, not the admin.
- [x] Skip related videos longer than 20 minutes, so a short-song session does not run into a two-hour concert.

### Admin from the player
- [x] Orange button opens `/admin/` in a full-page frame over the player, with a Close button. The PIN page appears if not logged in. Music keeps playing underneath.

## Steps

- [x] Plan (this file), agreed with Xavi.
- [x] Database: loved, looked-up column. Tests.
- [x] Server: love, related, browse home, remove player search. Tests.
- [x] MusicBrainz tagger with tests (recorded answers, no network in tests).
- [x] Player: top bar, full screen, heart, admin frame, endless play. ES5 check.
- [x] Check in Chrome.
- [ ] Xavi checks on the iPad.
- [x] README, review.

## Risks

- MusicBrainz will not know many concert recordings and home videos; those get no automatic tags. Titles like "Jordi Savall - Folías de España" match well; "A Unique Dearborn Double Bass at Upton Bass" will not.
- Related songs are chosen by YouTube. For a kids' player that is the main risk; the setting turns it off.

## Review

Built on 2026-11-07. 57 tests pass (14 new: loved songs, the database upgrade, the tagger with recorded MusicBrainz answers, related songs, the new pages, search gone).

Checked in Chrome against the real server:
- Home shows Lists (Loved, Played lately, All songs) and Tags; no search field, no "Music" button.
- The heart marked the playing song as loved, and the Loved tile counted it.
- Full screen showed only the video; a tap showed Small, the red heart and the play buttons.
- At the end of the queue (a concert, skipped to its last seconds), 5 related baroque concertos were added and the first one played.
- The Admin button opened the PIN page over the player while the music kept playing; "Back to the music" closed it.

Automatic tags on the real library: Savall recordings got "Jordi Savall, classical, baroque", one also "Hespèrion XXI". One wrong guess: a concert titled "Le Concert Spirituel" was tagged with the ensemble of that name.

Changes on the way:
- Song titles are often also the name of some band on MusicBrainz ("Bohemian Rhapsody" is a Queen cover band). An artist now counts only if MusicBrainz lists genres for it; those bands have none.
- MusicBrainz answers "503" when asked too often; the tagger waits and retries.
- In the admin panel, the admin's "Player" link would have opened a second player inside the panel; it now closes the panel.

Tagging takes about 30 s per song, as MusicBrainz allows one request a second and a title needs several guesses. The first run over 45 songs takes about 20 minutes, once.

## Follow-up (2026-11-07): tags from Claude instead of MusicBrainz

Xavi preferred a model to a name lookup. Tags now come from a bare `claude -p` call (Haiku, no tools, settings or MCP servers) with titles, channels and the existing tags, 10 songs per call. The 45-song library took 26 s, against about 30 s per song with MusicBrainz, and the tags were right where MusicBrainz guessed wrong or found nothing.

- The 76 MusicBrainz tags were removed, using the server log, which lists each one it added. None of Xavi's tags were touched (he had none on those songs).
- Bug found and fixed: Claude reused the tag "related" from the existing list on 13 library songs. That tag is the player's own; it is now kept out of the list sent to Claude and out of its answers, with a test, and the 13 were removed.
- The player offers only tags on more than one song.
- The green ticks on "+" now follow the queue, so Clear removes them. Checked in Chrome: 5 ticks before Clear, 0 after.

55 tests pass.
