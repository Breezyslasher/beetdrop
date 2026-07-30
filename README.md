# Trackpull

A mobile-first web app with one job: search for a song, download the audio
from YouTube Music, and drop it into a beets-flask inbox folder so beets
can tag and file it.

The line is the inbox. Upstream of it is Trackpull. Downstream of it is
beets. Trackpull does no MusicBrainz lookups, no release disambiguation,
no cover art, no library path construction and no lyrics — beets already
does every one of those things better, and beets-flask already provides a
mobile web UI for confirming its matches.

Current status: Phase 1 (CLI, no web layer). FastAPI, the UI, and the
Docker image follow in later phases.

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
python -m trackpull version
```

Environment variables: `INBOX_PATH`, `TRACKPULL_FORMAT`,
`TRACKPULL_BITRATE` (mp3 only), `TRACKPULL_COOKIES` (path to a cookies
file for throttled or region-locked content), `TRACKPULL_SCRATCH`.

FLAC and WAV are not offered: YouTube Music's source ceiling is roughly
160 kbps Opus or 256 kbps AAC, so a lossless container would be a larger
file carrying no additional information and would make the library
misreport its own quality.

## Tests

```
python -m pytest tests/
```

## Legal note

The tool downloads audio from YouTube. Users are responsible for ensuring
their use complies with applicable law and platform terms.
