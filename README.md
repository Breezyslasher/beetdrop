# Trackpull

A mobile-first web app with one job: search for a song, download the audio
from YouTube Music, and drop it into a beets-flask inbox folder so beets
can tag and file it.

The line is the inbox. Upstream of it is Trackpull. Downstream of it is
beets. Trackpull does no MusicBrainz lookups, no release disambiguation,
no cover art, no library path construction and no lyrics — beets already
does every one of those things better, and beets-flask already provides a
mobile web UI for confirming its matches.

Current status: Phase 2 (CLI plus web API). The UI and the Docker image
follow in later phases.

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
`TRACKPULL_PASSWORD`), supplied as an `X-Trackpull-Password` header or a
`password` query parameter (for EventSource). `/api/health` stays open
so container healthchecks work. No TLS: this is a LAN tool that sits
behind whatever reverse proxy already exists.

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
python -m pytest tests/
```

## Legal note

The tool downloads audio from YouTube. Users are responsible for ensuring
their use complies with applicable law and platform terms.
