# Beetdrop

A mobile-first, self-hosted web app: search YouTube Music for a song or
album, download the audio, match it against MusicBrainz, and file it -
fully tagged, with cover art - straight into your music library.
Standalone: no beets, no external tagger, one container.

## What a grab does

1. Downloads bestaudio (opus by default; m4a and mp3 offered; the
   stream is copied without re-encoding when the source codec already
   matches). No FLAC or WAV: the source tops out around 160 kbps Opus /
   256 kbps AAC, and a lossless container would only misreport quality.
2. Matches against MusicBrainz. Singles use duration-first recording
   scoring - more than 8 seconds off is rejected outright, live/remix
   qualifiers must agree on both sides, artist and title compare after
   normalisation - plus release selection that prefers the earliest
   official studio release over compilations and live albums. Albums
   are matched as one release (title/artist/track-count scoring), then
   the tracklists are aligned by album order and verified by durations.
3. Writes full Picard-compatible tags: artist credits with joinphrases,
   album, date, track and disc numbers with totals, and MusicBrainz
   recording/release/release-group/artist IDs in the locations Picard
   uses - so Plex, Navidrome, and Picard all agree with the files.
4. Embeds cover art from the Cover Art Archive (release, then release
   group, then the YouTube thumbnail as last resort) and writes
   cover.jpg into the album folder.
5. Files atomically into the library as
   {albumartist}/{album} ({year})/{disc-}NN - {title}.{ext}, never
   overwriting - collisions get a " (2)" suffix.
6. Fetches synced (timed) lyrics and writes a .lrc sidecar next to the
   track - the format Plex, Navidrome, and most players read for synced
   lyrics. Three sources: LRCLIB, Musixmatch, and Apple Music. Settings
   picks the primary provider and the others follow as fallbacks;
   whichever returns timed lyrics first wins. Timed only: results with
   only plain text are skipped. Musixmatch needs a session token - fetch
   the latest one with a button in Settings (same as updating yt-dlp), or
   paste your own. Apple Music has the best coverage by a distance,
   including older catalogue the other two miss entirely, and is the only
   source with per-word timing; it needs the media-user-token of an
   account with an active subscription. Either paste one, or sign in to
   Apple from Settings and Beetdrop mints it for you (SRP + 2FA). The
   Apple ID password is used only for the sign-in exchange - never
   stored, never logged, and never sent to Apple, since SRP proves you
   know it without transmitting it; only the session cookies persist
   (0600, in /config) so the token can be renewed later. Apple stays
   inert until a token is set. Per-word output (Enhanced/A2 LRC) is off by default
   because Plex, Navidrome, Jellyfin and Kodi are line-level and would
   show the inline tags as text - enable it with BEETDROP_WORD_LYRICS or
   in Settings if your player understands A2. Best effort, and toggleable
   in Settings (or BEETDROP_LYRICS). A "Fetch
   lyrics for existing library" button (CLI: `scan-lyrics`) backfills sidecars for
   tracks already in the library that have none yet, using the same
   source - it runs as a cancellable job in the queue. Artist and title
   come from the tags, falling back to the folder/file names
   ({Artist}/{Album}/{NN} - {Title} or "Artist - Title"); duration always
   comes from the decoded audio, so even untagged files can be matched.
   Placeholder lyrics are rejected: an LRC whose lines are perfectly
   evenly spaced is generated junk, never a real transcription, so it is
   never written. "Delete bad lyrics and re-fetch" (CLI: `scan-lyrics
   --refresh`) removes existing placeholder sidecars - whatever wrote
   them - and looks those tracks up again. The check is timing-only, so
   real lyrics in any language, romaji included, are never deleted.
   Every sidecar Beetdrop writes records where it came from, in the
   standard `[re:]` LRC tag players already ignore:
   `[re:beetdrop 0.62.0 apple word]` - the build, the source, and whether
   the file ended up word-by-word or line-level. Nothing on disk used to
   say, which is why repairing a rendering defect meant re-fetching every
   word-level file rather than the ones actually spoiled, and why "how
   many line-level tracks does Apple have no words for" had no answer.
   Stats breaks the library down by source; a file written before this
   counts as "untagged", which means unrecorded and never "not Apple".
   Those can be partly recovered without asking any provider anything:
   "Tag what can be established" (CLI: `scan-lyrics --tag-sources`) walks
   the library offline and tags every word-by-word sidecar as Apple,
   since Apple is the only source with per-word timing - an inference
   from what the other two can do, not a guess. Line-level sidecars are
   left untagged, because all three produce those and nothing in the file
   tells them apart; a plausible source written in would later be read as
   a fact. Those get tagged when a pass rewrites them. The build is
   recorded as `?`, since which version rendered an existing file cannot
   be recovered and the version is exactly what a repair pass reads. Two
   kinds of file are deliberately left undetermined rather than credited
   to Apple: one that already names its own writer in `[re:]`, and one
   with a `.lrc.bak` beside it, which is what an external forced aligner
   converting lyrics in place leaves behind. "Only Apple has word timing"
   stops being true the moment another tool writes word timing into the
   library, and a tag worth reading later is one that never guessed. Any
   writer's `[re:]` tag is understood, not just Beetdrop's, so a tool that
   names itself the same way shows up as its own source on Stats.
   Two kinds of unsound word timing are reported and can be filtered in
   Library. Timing that jumps *backwards* mid-line only comes from a bad
   conversion, and the upgrade pass repairs it. Timing that is *crowded* -
   several words within a few hundredths of a second, which nobody can
   sing - is what a forced aligner leaves on a line holding more words
   than its timing can fit: the tail gets pinned at the minimum step the
   writer allows. That one passes every other check, since the times
   still increase, the line timing is real and the words are right, so
   without this nothing found it and a word-by-word highlighter silently
   skips the words that share an instant. Both counts come out of the
   same read of each sidecar as everything else on those screens.

Grabs that cannot be verified against MusicBrainz are filed under
_review/ with YouTube-derived tags and an unverified marker, so the
clean library never gets polluted silently. The queue shows which
outcome each grab had.

MusicBrainz calls are rate limited to one per second process-wide and
cached in SQLite for 30 days. Honest caveat: there is no acoustic
fingerprinting, so a wrong-video grab (a cover, a re-upload) can only
be caught by duration and text similarity - watch the duration on the
cards and the _review folder.

## Albums

Search toggles between songs and albums (UI, `--albums` on the CLI,
`type=albums` on the API). Individual track failures do not abort an
album: the rest is filed and the job records which tracks failed and
why (including tracks YouTube Music serves without a videoId). Track
numbering preserves gaps. An album that finished with gaps shows a
"Retry failed tracks" button that re-downloads only the failures and
slots them into the album folder.

## Music videos

A third search mode, Videos (UI tab, `--videos` on the CLI, `type=videos`
on the API), grabs music videos instead of audio. Each is downloaded as an
mp4 (video+audio merged, capped at 1080p by default - configurable up to
4K or uncapped), best-effort matched against MusicBrainz for a clean
artist/title/album, and filed Kodi-style:

```
{video library}/{Artist}/{Artist} - {Title}.mp4
                        /{Artist} - {Title}.nfo        <- Kodi <musicvideo>
                        /{Artist} - {Title}-poster.jpg
```

The `.nfo` and poster are the exact names Kodi's Music Videos scraper
reads, so a scan needs no network lookup. Unlike an audio grab, a video
that does not match MusicBrainz is still filed (the video is the point);
the match only enriches its metadata.

Videos are filed to a **separate** library from your audio, because Kodi
treats Music and Music Videos as two library types with their own sources
and scrapers - mixing them in one folder is discouraged. By default the
video library is a `Music Videos` subfolder of `/music`, so no extra mount
is needed; set `VIDEO_PATH` (or the Settings field) to put it on its own
disk. Max quality is set with `BEETDROP_VIDEO_MAX_HEIGHT` or in Settings.

### A song's video, beside the song (Plex)

Plex is the other way round: it reads music videos inside a music
library, as extras linked to the track they belong to. Tick **+ music
video** on the Songs search and a grab also finds that song's video and
files it in the album folder, named for the audio file with Plex's
`-video` suffix:

```
Artist/Album (2013)/03 - Song.opus
Artist/Album (2013)/03 - Song-video.mp4
```

Plex's rule ("Adding Local Artist and Music Videos") is that the video
must start with the track's exact filename and sit in the same folder;
the suffix is what marks it as a music video, so a bare `03 - Song.mp4`
is not one. Versions 0.65 to 0.67 named it for the title alone
(`Song-video.mp4`), which Plex links to nothing; those are renamed once,
in the background, the first time a newer version starts.

The video's own YouTube thumbnail is embedded as its cover art - the
full 1280x720 frame where YouTube has one, not the letterboxed 480x360
default - for players that read embedded art (Kodi, Infuse, VLC, file
browsers). Plex has no way to set artwork on a music video extra and
draws its own thumbnail from a frame, so it may not show this one.

Music videos filed before that - by Beetdrop or another tool - often
have the picture as a separate image beside them instead. **Repair ->
Embed artwork into music videos** copies it in: for each MP4 with no
embedded art, it looks for `<video>-thumb`, `<video>-poster` or
`<video>` as `.jpg`/`.png`, then the same names without a Plex suffix
such as `-video`. Videos that already have artwork are left alone, the
images stay where they are, and each video is rewritten on a copy that
replaces it in one step, so a pass that is stopped part-way leaves no
half-written file. It runs on the Queue and says how many it embedded,
how many had no image, and how many had an image MP4 cannot hold. An
image whose name was cut short - one name the start of the other - is
paired too, but only when it fits exactly one video. The finished job's
log says why any failed (permission denied, not a readable MP4...) and
lists a few videos with no image alongside the images in their folder,
which shows a naming it does not understand yet.

A video with no image beside it but a YouTube link in its tags - yt-dlp's
`--embed-metadata` writes one into the comment - or a yt-dlp style
`[videoid]` at the end of its name gets that video's own thumbnail
fetched instead. A WebM or Matroska file named `.mp4` is reported as
such: it plays, but has no place for MP4 cover art. **Convert them, then
embed artwork** (under the same button) remuxes each into the real MP4
its name claims - video and audio copied untouched, nothing re-encoded,
same name, tags and any YouTube link kept, subtitles inside dropped -
and checks the result is as long as the original before replacing it.
One whose codecs play poorly in MP4 (Vorbis audio, say) is left alone,
and the job log says why.

For videos with no image and no link, **Search YouTube for them, then
embed artwork** searches YouTube for each by its name - without a
leading "07 - ", with a "-Video" suffix dropped and a "-Lyric" one read
as "(Lyrics)", and with a bracket a length limit cut off ("[Official A")
closed rather than lost. A result's thumbnail is taken only when all of
these hold:

- the same kind of upload: a live, remix or "Version 3" cut, or a
  lyric, audio or visualizer upload, only for a file whose name says the
  same - otherwise the picture is of something else;
- the title says the same thing (90%+) once brackets, punctuation and
  labels like "Official Video" are set aside, with or without the
  result's artist in front;
- the length is the file's own: within 2.5 seconds, or 10 when the
  title is as good as identical (another upload of the same video).

Of several that pass, the closest in length. Anything less certain is
skipped, and the log lists a few with the closest result and what ruled
it out. Artwork found this way is marked in the file's tags. One search
a second; after five failed searches in a row it stops, since YouTube is
refusing, and says so.

Plex does not read a music video's embedded artwork - it makes its own
thumbnail from a frame. With Plex set up, **Send artwork to Plex** (on
the same page) uploads each music video's embedded artwork to Plex as
that video's thumbnail: it walks the artists of the music library, asks
Plex for each one's extras, maps each extra's file back to the file
here, and posts the image (`POST /library/metadata/{id}/thumb`, owner
only). Plex keeps every image uploaded to an item, so a picture already
sent is remembered and not sent again; a changed one is. If Plex refuses
five uploads in a row it stops and says so.

Finding the right video is strict on identity and loose on length. A
wrong video beside a track is worse than none, because Plex presents it
as that song, so anything that is not the same recording by the same
artist is refused: live, remix and acoustic takes, "Official Audio"
uploads (a still image over the track), lyric videos, visualizers,
covers by someone else. Length is allowed a long way over - videos carry
intros, dance breaks and credits - but not much under, since a shorter
video is a radio edit or a different song.

It is always best effort. The audio is what was asked for, so a video
that cannot be found or fetched never fails the grab; the job card says
what happened to it. Tracks only - an album would mean a search per
track, and most album tracks have no video. A grab that went to
`_review/` gets its video too; fixing the match later carries the video
along and renames it for the new filename. Stats counts these apart from
the Kodi-style library.

The `-video` naming follows Plex's documented convention for music
extras rather than a test against a live server, so it is worth one grab
and a look in Plex before relying on it. If Plex wants it spelled
differently, it is one function (`plex_video_filename` in
`companion.py`).

## Telling Plex

Plex notices new files on its own schedule - a periodic scan, or a
filesystem watcher that often does not fire across a network share or a
container mount - so a grab can sit on disk for hours before it appears.
Give Beetdrop your Plex server's address and token in Settings and, when
a job finishes, it asks Plex for a partial scan of exactly the folder it
filed into: the same request Sonarr and Lidarr make. Never the whole
library; an album is one scan, not one per track; a lyrics pass that
changed something scans the music library once at the end.

**Sign in with Plex** saves finding the token. It shows a four-character
code; enter it at [plex.tv/link](https://plex.tv/link) on any phone or
computer, then pick the server from the list. Beetdrop tries the
server's addresses - local ones first, plex.tv's relay last - and keeps
the first that answers, with that server's own access token. The
account-wide token plex.tv hands back is held in memory only until a
server is chosen (half an hour at most), and is never written to disk. Only servers
you own can be chosen: Plex lets only a server's owner ask it to scan,
so a server shared with you is listed but greyed out. **Sign out of
Plex** forgets the address and token and keeps the folder mapping. The
address and token can still be typed in by hand.

**Library to scan** lists Plex's libraries once it is connected. Left
on *Automatic*, every library holding the music folder is scanned - a
folder in two libraries, like one "Both" folder in two people's music
libraries, is scanned into each. Pick one and only that one is.

Beetdrop and Plex usually see the same files at different paths -
`/music` here, `/srv/.../Media/Music/explo` on the NAS. To scan just the
folder a grab landed in, Beetdrop needs to know which; it usually works
it out alone: in a container it reads which folder of the host's disk
is mounted as `/music` (`/proc/self/mountinfo`) and picks the Plex
folder ending the same way. "Where Plex sees the music folder" is only
for when that guess is wrong or impossible. With a library picked and
no folder known, each grab rescans that whole library instead - slower,
since Plex walks every folder, though it skips files it already has.
The token is stored on the server like the Apple token and never sent
back to the page.

Requests follow Plex's published OpenAPI description: `GET
/library/sections/all` to find the library, `POST
/library/sections/{id}/refresh?path=...` to scan the folder, token in the
`X-Plex-Token` header. A server older than that API, which only answers
the bare `/library/sections`, is handled too.

It is best effort: a Plex that is off or unreachable is noted on the job
card and never fails a grab. One Plex refusal worth knowing: a server
signed out of plex.tv will not refresh a music library, and the job card
says so rather than reporting a bare HTTP 503. Env: `BEETDROP_PLEX_URL`,
`BEETDROP_PLEX_TOKEN`, `BEETDROP_PLEX_MUSIC_PATH`,
`BEETDROP_PLEX_SECTION` (a library key).

## Fixing a wrong match

There is no acoustic fingerprinting, so a cover or a same-length
different song can get through. Two kinds go wrong: a grab that could
not be verified is filed under `_review/` with tags taken from YouTube,
and a grab that matched the *wrong* song confidently is filed as
verified and looks settled. Settings lists the first and searches the
library for the second - by whatever it is wrongly called, which is what
shows in your player.

Pick the right recording and Beetdrop re-tags the file and moves it to
where those tags say it belongs; the .lrc beside it comes too, since
lyrics are matched to the audio rather than the tags. Candidates are
shown as MusicBrainz returned them, unscored: the automatic checks are
what got it wrong, so applying them again would hide the right answer.

The album box is optional. Filled in, it narrows the search to
recordings on a release of that name - the way to tell the album cut
from the single, a live take or a compilation when title and artist are
the same - and the track is filed under that album rather than the one
Beetdrop would pick (it prefers the original studio release). A name
with bracketed extras still matches, so "Rumours" finds "Rumours
(Deluxe Edition)". If MusicBrainz spells the album differently, leave it
out.
Tracks only for now; albums are a bigger job.

## Lyrics on their own

A fourth search mode, Lyrics, searches Apple Music and saves the .lrc
straight to the device - word-by-word by default. Nothing is matched
against the library and nothing is written to it: it is just the lyrics
file. Preview it first if you want to see what you are getting; a track
Apple has only line timing for still downloads, and the page says so.

## The Workbench layout

The UI is a shell rather than one page: a sidebar on desktop
(Search / Queue / Library / Stats / Repair / Settings) and a bottom tab
bar on phone, with the queue as a persistent right rail instead of a
sheet you have to open. Nothing floats any more - the old top brand bar,
the fixed queue footer, the search bar pinned to the lower half of a
phone screen, and the very long Settings sheet are all gone; Settings is
a destination like any other, and on phone the queue is a one-line strip
above the tab bar. Which shape you get is the Layout setting (auto
follows screen size at 900px), and that one class is the only thing that
decides it. Two screens are new:

- **Library** - what is on disk, as albums, with filter chips for the
  maintenance lists that used to be buried in Settings: missing lyrics,
  line-level only, unverified, gaps in numbering, junk. Expanding an
  album lists its tracks with per-track lyrics state. Rows show the
  cover.jpg from the album folder, served by the app since the library
  mount is not a path the browser can reach; an album without one keeps
  the stripe placeholder rather than a broken image.
- **Stats** - library health: coverage, formats, grab reliability and a
  14-day activity band, with the lyrics passes labelled by how many
  tracks each would touch, and the sidecars broken down by which source
  wrote them.

Both are read on demand rather than cached. At 5,794 tracks the walk plus
a read of every sidecar measures well under a second, and a second copy
of the truth is a thing that can go stale. One walk and one read per
sidecar: state, source and timing faults all come out of the same parse,
which is what the timing counts used to need a second walk for. Embedded tags are deliberately
not read: a mutagen open per file is the one part that is not free, and
nothing on these screens needs it.

Album completeness comes from the numbering in the filenames, not from
MusicBrainz - "gaps in numbering" is what an interrupted album grab
actually leaves, and the release total would mean reading a release id
out of every file.

## Web UI and API

`python -m beetdrop serve` (port 8090) serves a single-page PWA:
search cards with prominent durations, a live queue (SSE) with cancel,
retry, per-job logs, and progress, and a settings panel. Duplicate
grabs are detected and confirmed before re-downloading. Passwords are
stored hashed, sessions ride an HttpOnly cookie, failed logins are
throttled (Cloudflare-tunnel aware), and yt-dlp can be updated live
from Settings with no restart.

```
GET  /api/search?q=&type=songs|albums|videos
POST /api/grab                    {"video_id", "kind", "format", "force"}
GET  /api/jobs                    POST /api/jobs/{id}/retry|cancel
GET/PUT /api/settings             GET /api/health   GET /events (SSE)
POST /api/login                   POST /api/ytdlp/update
POST /api/lyrics/musixmatch-token POST /api/lyrics/scan
POST /api/lyrics/tag-sources      GET  /api/library  GET /api/stats
POST /api/plex/test               POST /api/plex/sign-out
POST /api/plex/pin                GET  /api/plex/pin/{id}
POST /api/plex/pin/{id}/server     POST /api/videos/embed-art
POST /api/plex/send-artwork
```

## Running it

```
docker compose up -d
```

See docker-compose.yml: mount /config (state) and /music (your
library), set PUID/PGID to the library owner. Environment variables:
`MUSIC_PATH`, `BEETDROP_CONFIG`, `BEETDROP_FORMAT`, `BEETDROP_BITRATE`,
`BEETDROP_PASSWORD`, `BEETDROP_COOKIES`, `BEETDROP_CONCURRENCY` (1-4),
`BEETDROP_LYRICS`, `BEETDROP_LYRICS_PROVIDER` (lrclib|musixmatch|apple),
`BEETDROP_MXM_TOKEN` (Musixmatch token), `BEETDROP_APPLE_TOKEN`
(Apple Music media-user-token), `BEETDROP_APPLE_STOREFRONT` (default us),
`BEETDROP_WORD_LYRICS` (per-word timing, default off), `BEETDROP_PLEX_URL`,
`BEETDROP_PLEX_TOKEN`, `BEETDROP_PLEX_MUSIC_PATH` (scan Plex after each
grab), `VIDEO_PATH` (music-video
library; defaults to a subfolder of /music), `BEETDROP_VIDEO_MAX_HEIGHT`
(video quality cap in px, default 1080, 0 = uncapped), `BEETDROP_TRACK_DELAY`,
`BEETDROP_MIN_FREE_MB`, `BEETDROP_KEEP_JOBS`, `BEETDROP_KEEP_DAYS`,
`BEETDROP_SCRATCH`. Legacy `TRACKPULL_*` names are still honored.

CLI - in Docker, run it inside the container as the library's owner, so
nothing it writes ends up owned by root:

```
docker exec -u beetdrop beetdrop python3 -m beetdrop scan-lyrics --stats
```

`docker exec` takes the container name (`beetdrop` by default - `docker ps`
if you changed it) and works from any directory. `docker compose exec` is
equivalent but only inside the folder holding your docker-compose.yml,
otherwise it exits with "no configuration file provided: not found".
The `-u` matters either way: the entrypoint drops to the beetdrop user,
but exec defaults to root, and a file written as root into a library the
host owns is a permissions problem to untangle later.

```
python -m beetdrop search "artist song" [--albums | --videos]
python -m beetdrop grab <video_id> [--album | --video] [--format opus|m4a|mp3] [--library PATH]
python -m beetdrop lyrics-probe "Artist" "Title" [--duration N]  # why a track is line-level
python -m beetdrop scan-lyrics [--refresh|--upgrade|--redo-words|--stats]  # backfill .lrc
                                               # --refresh purges junk first
                                               # --upgrade re-fetches line-level as word-by-word
                                               # --redo-words re-renders every word-by-word .lrc
                                               # --tag-sources tags existing sidecars, offline
python -m beetdrop richsync-probe [--sample N] [--verify N]  # is Musixmatch
                                               # worth adding as a 2nd word source?
python -m beetdrop serve [--host 0.0.0.0] [--port 8090]
```

History: this project previously fed a beets/beets-flask inbox
("Trackpull", then Beetdrop inbox mode). That pipeline was removed in
0.11.0 - the last inbox-capable image is 0.10.x.

## Tests

```
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests/
```

## Legal note

The tool downloads audio from YouTube. Users are responsible for
ensuring their use complies with applicable law and platform terms.
