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

## Web UI and API

`python -m beetdrop serve` (port 8090) serves a single-page PWA:
search cards with prominent durations, a live queue (SSE) with cancel,
retry, per-job logs, and progress, and a settings panel. Duplicate
grabs are detected and confirmed before re-downloading. Passwords are
stored hashed, sessions ride an HttpOnly cookie, failed logins are
throttled (Cloudflare-tunnel aware), and yt-dlp can be updated live
from Settings with no restart.

```
GET  /api/search?q=&type=songs|albums
POST /api/grab                    {"video_id", "kind", "format", "force"}
GET  /api/jobs                    POST /api/jobs/{id}/retry|cancel
GET/PUT /api/settings             GET /api/health   GET /events (SSE)
POST /api/login                   POST /api/ytdlp/update
```

## Running it

```
docker compose up -d
```

See docker-compose.yml: mount /config (state) and /music (your
library), set PUID/PGID to the library owner. Environment variables:
`MUSIC_PATH`, `BEETDROP_CONFIG`, `BEETDROP_FORMAT`, `BEETDROP_BITRATE`,
`BEETDROP_PASSWORD`, `BEETDROP_COOKIES`, `BEETDROP_CONCURRENCY` (1-4),
`BEETDROP_TRACK_DELAY`, `BEETDROP_MIN_FREE_MB`, `BEETDROP_KEEP_JOBS`,
`BEETDROP_KEEP_DAYS`, `BEETDROP_SCRATCH`. Legacy `TRACKPULL_*` names
are still honored.

CLI:

```
python -m beetdrop search "artist song" [--albums]
python -m beetdrop grab <video_id> [--album] [--format opus|m4a|mp3] [--library PATH]
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
