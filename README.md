# Trackpull

A mobile-first web app with one job: search for a song, download the audio
from YouTube Music, and drop it into a beets-flask inbox folder so beets
can tag and file it.

The line is the inbox. Upstream of it is Trackpull. Downstream of it is
beets. Trackpull does no MusicBrainz lookups, no release disambiguation,
no cover art, no library path construction and no lyrics — beets already
does every one of those things better, and beets-flask already provides a
mobile web UI for confirming its matches.

Current status: CLI, web API, web UI (PWA), and Docker image.

## What a grab does

1. Downloads bestaudio for the video into a scratch directory outside the
   inbox and extracts to the target format (opus by default; m4a and mp3
   offered; the stream is copied without re-encoding when the source codec
   already matches).
2. Verifies the file exists, is non-empty, and opens as valid audio.
3. Writes seed tags from YouTube Music's structured search result: title,
   artist, albumartist, album, and nothing else. No date, no genre, no
   MBIDs, no cover art — a wrong guess here actively degrades beets'
   matching, and beets will overwrite all of it anyway. Track number is
   omitted rather than invented.
4. Moves the whole per-grab folder (`<Artist> - <Title>`, sanitised, capped
   at 200 bytes per segment) into the inbox with a single atomic rename, so
   a watching beets-flask never observes a partially written folder.
   Existing folders are never overwritten; collisions get a ` (2)` suffix.

## Albums

Search can be switched between songs and albums (UI toggle, `--albums`
on the CLI, `type=albums` on the API). Grabbing an album downloads
every playable track, seed-tags each with its track number (known from
the album order, so it is written here unlike single grabs), names the
files `NN - Title.ext`, and delivers the whole thing as ONE album
folder in a single atomic rename - a complete album folder is beets'
best import unit and matches full releases far better than singletons.

Individual track failures do not abort the album: the rest is
delivered and the job records which tracks failed and why (including
tracks YouTube Music serves without a videoId). Track numbering
preserves gaps, so a missing track 5 does not shift track 6. Only an
album with zero successful tracks fails.

An album that finished with gaps shows a "Retry failed tracks" button:
the retry re-downloads only the tracks that failed. If the album folder
is still in the inbox, recovered tracks are patched into it (one atomic
replace per file, never overwriting), so beets sees one complete album
before importing; if beets already took the folder, the recovered
tracks are delivered as a new album folder and beets merges them into
the same release on import. A retry that recovers nothing leaves the
job done and the delivered folder untouched - permanently unavailable
tracks stay listed so you know what is missing.

```
python -m trackpull search "artist album" --albums
python -m trackpull grab <browse_id> --album
```

Success means the file was handed off to the inbox, not that it was
imported — whether it imports is beets' decision.

## Requirements

- Python 3.12 (3.11 works)
- ffmpeg on PATH

```
pip install -r requirements.txt
```

## Usage

```
export INBOX_PATH=/path/to/beets/inbox

python -m trackpull search "artist song title"
python -m trackpull grab <video_id> [--format opus|m4a|mp3] [--inbox PATH]
python -m trackpull serve [--host 0.0.0.0] [--port 8090]
python -m trackpull version
```

Environment variables: `INBOX_PATH`, `TRACKPULL_FORMAT`,
`TRACKPULL_BITRATE` (mp3 only), `TRACKPULL_COOKIES` (path to a cookies
file for throttled or region-locked content), `TRACKPULL_SCRATCH`,
`TRACKPULL_CONFIG` (state directory), `TRACKPULL_PASSWORD`.

## Web UI

`serve` also serves a single-page UI at the root: search with result
cards (thumbnail, title, artist, album, prominent duration so a live
version or ten-minute extended mix is visible before grabbing), a queue
bar showing the active-download count that expands into the full queue,
and a settings panel (format, bitrate, inbox path, password, layout,
read-only yt-dlp version). Job state streams in live over SSE and the
queue survives page reloads.

The layout setting switches between three modes: Auto (follows screen
size), Mobile (search field and primary action in the lower half of the
screen for one-handed phone use), and Desktop (the same page in a
max-width container with the search bar at the top). The choice is
stored in the browser.

The page is installable as a PWA (standalone display, maskable icons);
a minimal service worker caches the shell only, never API responses.
Vue 3 is vendored as a single file - no build step, no CDN dependency
at runtime, so the app works on a LAN without internet access.

## Wiring beets-flask

beets-flask ships with a placeholder inbox ("/music/dummy"); until it
is replaced, it reports "Path /music/dummy does not exist or is no
directory". In beets-flask's beets config.yaml, point an inbox folder
at the same host directory Trackpull writes to, as mounted inside the
beets-flask container:

```
directory: /music/library

gui:
  inbox:
    folders:
      trackpull:
        name: "Trackpull inbox"
        path: /music/inbox
        autotag: preview
```

The container-side paths do not need to match Trackpull's /inbox; both
containers just have to mount the same host folder. Keep PUID/PGID
consistent between the two containers so beets can move what Trackpull
writes.

## Handoff watching

After a grab lands, Trackpull keeps an eye on the folder it delivered -
watching the inbox only, never beets' database. When the folder leaves
the inbox the job shows "Picked up by beets". If it is still sitting
there after a grace period (5 minutes), the job is flagged "Not
auto-imported - review it in beets-flask" and the UI shows a
notification, since that usually means the match fell below the
auto-import threshold and is waiting in beets-flask for review. This is
a heuristic: a folder can also leave the inbox because it was deleted
by hand, and with auto-import disabled every grab will flag for review
once the grace period passes.

## Web API

`serve` runs the FastAPI app:

```
GET  /api/search?q=&limit=        songs search, normalised results
POST /api/grab                    {"video_id": ..., "format": ..., "bitrate": ...}
GET  /api/jobs                    recent jobs from SQLite
POST /api/jobs/{id}/retry         re-run a failed job
GET  /api/settings                includes read-only yt-dlp version
PUT  /api/settings                output_format, bitrate, inbox, password
GET  /api/health                  inbox writability, yt-dlp version
GET  /events                      SSE stream of job state changes
```

Downloads run in a background worker capped at two concurrent grabs;
searches run separately and never wait behind a download. Job state
persists in SQLite, so the queue survives restarts (jobs that were
mid-flight when the process died are marked failed and can be retried).
Stages: queued, searching, downloading, extracting, tagging, moving,
done, failed.

Auth is an optional single shared password (set via settings or
`TRACKPULL_PASSWORD`), hardened enough to sit behind a Cloudflare
tunnel or other internet-facing proxy:

- The password is stored as a salted PBKDF2 hash, never plaintext (a
  plaintext password stored by an earlier version is hashed in place on
  startup), and every comparison is constant-time.
- Browsers log in once via `POST /api/login` and hold a signed HttpOnly
  session cookie for 30 days; the password is never kept in the browser
  and never appears in a query string (query strings end up in access
  logs - the old `?password=` parameter is gone). Changing the password
  invalidates every session.
- Scripts and curl use the `X-Trackpull-Password` header.
- Failed attempts are throttled per client (5 in 15 minutes, keyed by
  `CF-Connecting-IP` behind Cloudflare), after which logins answer 429.
- All responses carry nosniff/frame-deny/no-referrer headers, and shell
  assets are served `no-cache` so a stale cached app.js can never be
  mixed with fresh HTML.

`/api/health` stays open so container healthchecks work. TLS is the
tunnel or reverse proxy's job.

## Operations

- Jobs still queued when the process restarts simply run on startup;
  only jobs that were mid-download are marked failed (retryable).
- Grabs are refused up front when the inbox filesystem has less than
  `TRACKPULL_MIN_FREE_MB` (default 512) free, and /api/health reports
  the free space - running out of disk mid-album otherwise surfaces as
  a confusing error after the download already happened.
- /api/health reports failures_last_hour; a wave of failures usually
  means YouTube changed something and yt-dlp needs updating. Settings
  has an "Update yt-dlp" button (POST /api/ytdlp/update); the new
  version loads on the next container restart, and the response says
  so rather than pretending.
- Failed jobs keep the last lines of yt-dlp output, shown as an
  expandable log in the queue.
- The format picker next to the search field overrides the output
  format for individual grabs without touching the saved setting.
- Queued and running jobs have a Cancel button; a running job stops at
  its next progress update. Cancelled jobs can be retried.
- Grabbing something already grabbed (or currently in flight) answers
  409 with the existing job; the UI asks before grabbing again, and the
  API takes force: true to override.
- Terminal jobs are pruned once they are BOTH older than
  TRACKPULL_KEEP_DAYS (default 30) and beyond the newest
  TRACKPULL_KEEP_JOBS (default 200).
- Album tracks download with a randomized pause between them
  (TRACKPULL_TRACK_DELAY, default "2-5" seconds) - back-to-back
  downloads look bot-like to YouTube's throttling.
- Cookies for throttled or region-locked videos can be pasted straight
  into Settings (stored as a file in /config, mode 600); an uploaded
  cookie file wins over the TRACKPULL_COOKIES mount. Clearing removes
  the file.
- Concurrent download workers are settable 1-4 (Settings or
  TRACKPULL_CONCURRENCY, default 2); the change applies on the next
  container restart.

FLAC and WAV are not offered: YouTube Music's source ceiling is roughly
160 kbps Opus or 256 kbps AAC, so a lossless container would be a larger
file carrying no additional information and would make the library
misreport its own quality.

## Docker

```
docker compose up -d
```

See docker-compose.yml for the volume and PUID/PGID wiring; the inbox
volume must resolve to the same path beets-flask watches. The image is
published to GitHub Container Registry as
`ghcr.io/breezyslasher/trackpull` (latest and per-version tags) by the
publish-docker workflow on every push to main that touches the app or
the Dockerfile. Set `TRACKPULL_SELFUPDATE=1` to refresh yt-dlp at
container start; the resolved version is always visible in
/api/settings and /api/health.

## Tests

```
pip install -r requirements-dev.txt
python -m pytest tests/
```

## Legal note

The tool downloads audio from YouTube. Users are responsible for ensuring
their use complies with applicable law and platform terms.
