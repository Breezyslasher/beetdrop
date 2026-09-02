"""Library mode: resolve YouTube Music candidates against MusicBrainz and
file finished audio straight into the music library.

Layout: {albumartist}/{album} ({year})/{disc-}{NN} - {title}.{ext}
Unmatched grabs are filed under _review/ with YouTube-derived tags and
an unverified marker, so nothing silently pollutes the clean library.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .fulltags import FullTags
from .matching import (
    best_recording,
    credit_ids,
    credit_name,
    score_release,
    select_release,
)
from .paths import sanitize_segment

REVIEW_DIR = "_review"
ALBUM_MATCH_THRESHOLD = 65
# Fraction of album tracks whose durations must agree with the MB
# tracklist for the album match to hold.
ALBUM_DURATION_AGREEMENT = 0.5


def _flatten_media(release: dict) -> list[dict]:
    """MB tracks in album order, each annotated with disc numbering."""
    media = release.get("media") or []
    disc_total = len(media) or 1
    flat = []
    for medium in media:
        disc_number = int(medium.get("position") or 1)
        tracks = medium.get("tracks") or []
        for track in tracks:
            flat.append({
                "track": track,
                "disc_number": disc_number,
                "disc_total": disc_total,
                "track_total": int(medium.get("track-count") or len(tracks)),
            })
    return flat


def _release_date(release: dict) -> str:
    return (release.get("date")
            or (release.get("release-group") or {}).get("first-release-date")
            or "")


def _tags_from_release_track(release: dict, entry: dict,
                             fallback_title: str, fallback_artist: str) -> FullTags:
    track = entry["track"]
    recording = track.get("recording") or {}
    artist_credit = (track.get("artist-credit")
                     or recording.get("artist-credit")
                     or release.get("artist-credit"))
    return FullTags(
        title=track.get("title") or recording.get("title") or fallback_title,
        artist=credit_name(artist_credit) or fallback_artist,
        album_artist=credit_name(release.get("artist-credit")) or fallback_artist,
        album=release.get("title", ""),
        date=_release_date(release),
        track_number=int(track.get("position") or 0),
        track_total=entry["track_total"],
        disc_number=entry["disc_number"],
        disc_total=entry["disc_total"],
        recording_mbid=recording.get("id", ""),
        release_mbid=release.get("id", ""),
        release_group_mbid=(release.get("release-group") or {}).get("id", ""),
        artist_mbids=credit_ids(artist_credit),
        unverified=False,
    )


@dataclass
class TrackResolution:
    tags: FullTags
    matched: bool
    release: Optional[dict] = None  # full release, for cover art


def resolve_track(mb, title: str, artist: str,
                  duration_seconds: Optional[int]) -> TrackResolution:
    """Match one candidate to a MusicBrainz recording and pick which
    release it files under."""
    recordings = mb.search_recordings(title, artist)
    recording, _score = best_recording(title, artist, duration_seconds, recordings)
    if recording is None:
        return TrackResolution(
            tags=FullTags(title=title, artist=artist or "Unknown Artist",
                          album_artist=artist or "Unknown Artist",
                          album=title, unverified=True),
            matched=False,
        )
    chosen = select_release(recording.get("releases") or [])
    if chosen is None:
        # A recording with no usable release: keep the identity, file as
        # a single named after itself.
        return TrackResolution(
            tags=FullTags(
                title=recording.get("title", title),
                artist=credit_name(recording.get("artist-credit")) or artist,
                album_artist=credit_name(recording.get("artist-credit")) or artist,
                album=recording.get("title", title),
                recording_mbid=recording.get("id", ""),
                artist_mbids=credit_ids(recording.get("artist-credit")),
                unverified=False,
            ),
            matched=True,
        )
    release = mb.get_release(chosen["id"])
    for entry in _flatten_media(release):
        if (entry["track"].get("recording") or {}).get("id") == recording.get("id"):
            return TrackResolution(
                tags=_tags_from_release_track(release, entry, title, artist),
                matched=True, release=release)
    # Recording not on the fetched release (data drift): fall back to
    # release-level tags without numbering.
    return TrackResolution(
        tags=FullTags(
            title=recording.get("title", title),
            artist=credit_name(recording.get("artist-credit")) or artist,
            album_artist=credit_name(release.get("artist-credit")) or artist,
            album=release.get("title", ""),
            date=_release_date(release),
            recording_mbid=recording.get("id", ""),
            release_mbid=release.get("id", ""),
            release_group_mbid=(release.get("release-group") or {}).get("id", ""),
            artist_mbids=credit_ids(recording.get("artist-credit")),
        ),
        matched=True, release=release)


@dataclass
class AlbumResolution:
    release: dict
    # YouTube album-order track number -> FullTags from the MB tracklist
    track_tags: dict = field(default_factory=dict)


def resolve_album(mb, album_title: str, album_artist: str,
                  yt_tracks: list) -> Optional[AlbumResolution]:
    """Match a whole YouTube album to one MusicBrainz release and align
    the tracklists by album order, verified by durations."""
    candidates = mb.search_releases(album_title, album_artist)
    scored = sorted(
        ((score_release(album_title, album_artist, len(yt_tracks), release), release)
         for release in candidates),
        key=lambda pair: pair[0], reverse=True)
    if not scored or scored[0][0] < ALBUM_MATCH_THRESHOLD:
        return None
    release = mb.get_release(scored[0][1]["id"])
    flat = _flatten_media(release)
    if not flat:
        return None

    # Align by album order: YT track_number N maps to the Nth MB track.
    by_order = {}
    agreements, comparisons = 0, 0
    for yt_track in yt_tracks:
        index = (yt_track.track_number or 0) - 1
        if not 0 <= index < len(flat):
            continue
        entry = flat[index]
        mb_length = (entry["track"].get("length")
                     or (entry["track"].get("recording") or {}).get("length"))
        if yt_track.duration_seconds and mb_length:
            comparisons += 1
            if abs(yt_track.duration_seconds - int(mb_length) / 1000.0) <= 8:
                agreements += 1
        by_order[yt_track.track_number] = entry
    if comparisons and agreements / comparisons < ALBUM_DURATION_AGREEMENT:
        return None  # tracklists disagree; this is not the same release

    resolution = AlbumResolution(release=release)
    for track_number, entry in by_order.items():
        resolution.track_tags[track_number] = _tags_from_release_track(
            release, entry, fallback_title="", fallback_artist=album_artist)
    return resolution


# -- filing ----------------------------------------------------------------


def album_dir(root: Path, tags: FullTags) -> Path:
    """{root}/{albumartist}/{album} ({year})/ - or _review/... when
    unverified."""
    artist_seg = sanitize_segment(tags.album_artist or tags.artist or "Unknown Artist")
    album_name = tags.album or tags.title or "Unknown Album"
    album_seg = sanitize_segment(
        "%s (%s)" % (album_name, tags.year) if tags.year else album_name)
    if tags.unverified:
        return root / REVIEW_DIR / sanitize_segment(
            "%s - %s" % (tags.artist or "Unknown Artist", tags.title))
    return root / artist_seg / album_seg


def track_filename(tags: FullTags, ext: str) -> str:
    title_seg = sanitize_segment(tags.title or "untitled")
    if tags.track_number:
        prefix = ("%d-" % tags.disc_number) if tags.disc_total > 1 else ""
        return "%s%02d - %s.%s" % (prefix, tags.track_number, title_seg, ext)
    return "%s.%s" % (title_seg, ext)


def place_file(source: Path, destination: Path) -> Path:
    """Copy-then-replace into the library, atomic at the destination and
    never overwriting: collisions get " (2)" before the extension."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    final = destination
    counter = 2
    while final.exists():
        final = destination.with_name(
            "%s (%d)%s" % (destination.stem, counter, destination.suffix))
        counter += 1
    temp = final.parent / (".%s.beetdrop-tmp" % final.name)
    try:
        shutil.copyfile(source, temp)
        os.replace(temp, final)
    finally:
        if temp.exists():
            temp.unlink()
    return final


def write_cover_file(directory: Path, cover: bytes, mime: str) -> None:
    """cover.jpg/png in the album folder - what Plex and friends expect."""
    ext = "png" if mime == "image/png" else "jpg"
    target = directory / ("cover.%s" % ext)
    if target.exists():
        return
    temp = directory / (".cover.beetdrop-tmp")
    try:
        temp.write_bytes(cover)
        os.replace(temp, target)
    finally:
        if temp.exists():
            temp.unlink()
