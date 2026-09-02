"""YouTube Music search via ytmusicapi, normalised to a flat shape.

Unauthenticated is fine; no OAuth in phase 1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ytmusicapi import YTMusic

from .cleaning import clean_title

DEFAULT_LIMIT = 8  # a phone screen shows about four cards


@dataclass
class Result:
    video_id: str
    title: str  # cleaned, for display and seed tags
    raw_title: str  # kept for debugging
    artists: list[str] = field(default_factory=list)
    album: Optional[str] = None
    duration_seconds: Optional[int] = None
    thumbnail_url: str = ""
    track_number: Optional[int] = None  # known only for album tracks

    @property
    def artist_display(self) -> str:
        return ", ".join(self.artists)

    @property
    def primary_artist(self) -> str:
        # The first credited artist - the right input for MusicBrainz
        # matching (better than the comma-joined display string).
        return self.artists[0] if self.artists else ""


@dataclass
class AlbumResult:
    browse_id: str
    title: str
    artists: list[str] = field(default_factory=list)
    year: str = ""
    album_type: str = ""  # Album / EP / Single, as YouTube Music labels it
    track_count: Optional[int] = None
    thumbnail_url: str = ""

    @property
    def artist_display(self) -> str:
        return ", ".join(self.artists)


@dataclass
class AlbumLookup:
    album: AlbumResult
    tracks: list[Result]
    # Tracks YouTube Music serves without a videoId, as
    # {"n": track_number, "title": title} so retries can target them.
    unavailable: list[dict] = field(default_factory=list)


def _duration_to_seconds(text) -> Optional[int]:
    try:
        parts = [int(p) for p in text.split(":")]
    except (ValueError, AttributeError):
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + part
    return seconds


def _row_duration(row: dict) -> Optional[int]:
    # ytmusicapi does not always populate duration_seconds; fall back to
    # parsing the "3:45" display text so the card never loses the one
    # field that exposes extended mixes and live cuts.
    duration = row.get("duration_seconds")
    if duration is None:
        duration = _duration_to_seconds(row.get("duration", ""))
    return duration


def _normalise(row: dict) -> Optional[Result]:
    video_id = row.get("videoId")
    if not video_id:
        return None
    raw_title = row.get("title", "")
    artists = [a.get("name", "") for a in row.get("artists") or [] if a.get("name")]
    album = None
    if isinstance(row.get("album"), dict):
        album = row["album"].get("name") or None
    thumbnails = row.get("thumbnails") or []
    return Result(
        video_id=video_id,
        title=clean_title(raw_title),
        raw_title=raw_title,
        artists=artists,
        album=album,
        duration_seconds=_row_duration(row),
        thumbnail_url=thumbnails[-1]["url"] if thumbnails else "",
    )


def search_songs(query: str, limit: int = DEFAULT_LIMIT) -> list[Result]:
    yt = YTMusic()
    rows = yt.search(query, filter="songs", limit=limit)
    results = []
    for row in rows:
        result = _normalise(row)
        if result:
            results.append(result)
        if len(results) >= limit:
            break
    return results


def search_videos(query: str, limit: int = DEFAULT_LIMIT) -> list[Result]:
    """Music videos, normalised to the same Result shape as songs. Each
    carries a standard YouTube videoId that grabs as a music video."""
    yt = YTMusic()
    rows = yt.search(query, filter="videos", limit=limit)
    results = []
    for row in rows:
        result = _normalise(row)
        if result:
            results.append(result)
        if len(results) >= limit:
            break
    return results


def _parse_album_row(row: dict) -> Optional[AlbumResult]:
    browse_id = row.get("browseId")
    if not browse_id:
        return None
    artists = [a.get("name", "") for a in row.get("artists") or [] if a.get("name")]
    thumbnails = row.get("thumbnails") or []
    return AlbumResult(
        browse_id=browse_id,
        title=row.get("title", ""),
        artists=artists,
        year=str(row.get("year") or ""),
        album_type=row.get("type", ""),
        thumbnail_url=thumbnails[-1]["url"] if thumbnails else "",
    )


def search_albums(query: str, limit: int = DEFAULT_LIMIT) -> list[AlbumResult]:
    yt = YTMusic()
    rows = yt.search(query, filter="albums", limit=limit)
    results = []
    for row in rows:
        album = _parse_album_row(row)
        if album:
            results.append(album)
        if len(results) >= limit:
            break
    return results


def lookup_album(browse_id: str) -> AlbumLookup:
    """Resolve an album browseId into its metadata and playable tracks.

    Tracks YouTube Music serves without a videoId (region-locked or
    delisted) are reported in `unavailable` rather than silently dropped.
    """
    yt = YTMusic()
    data = yt.get_album(browse_id)
    artists = [a.get("name", "") for a in data.get("artists") or [] if a.get("name")]
    thumbnails = data.get("thumbnails") or []
    album = AlbumResult(
        browse_id=browse_id,
        title=data.get("title", ""),
        artists=artists,
        year=str(data.get("year") or ""),
        album_type=data.get("type", ""),
        track_count=data.get("trackCount"),
        thumbnail_url=thumbnails[-1]["url"] if thumbnails else "",
    )
    tracks: list[Result] = []
    unavailable: list[dict] = []
    for index, row in enumerate(data.get("tracks") or [], start=1):
        raw_title = row.get("title", "")
        if not row.get("videoId"):
            unavailable.append({"n": index, "title": raw_title or ("track %d" % index)})
            continue
        track_artists = [a.get("name", "") for a in row.get("artists") or [] if a.get("name")]
        duration = _row_duration(row)
        tracks.append(Result(
            video_id=row["videoId"],
            title=clean_title(raw_title),
            raw_title=raw_title,
            artists=track_artists or artists,
            album=album.title,
            duration_seconds=duration,
            thumbnail_url=album.thumbnail_url,
            track_number=index,
        ))
    return AlbumLookup(album=album, tracks=tracks, unavailable=unavailable)


def lookup_video(video_id: str) -> Result:
    """Resolve a videoId into a Result.

    get_song supplies title, channel author, and duration. A follow-up
    songs-filtered search for the same videoId recovers the structured
    artist list and album when YouTube Music has them; the seed tags are
    materially better when it succeeds, so it is worth one extra request.
    """
    yt = YTMusic()
    song = yt.get_song(video_id)
    details = song.get("videoDetails") or {}
    raw_title = details.get("title", "")
    author = details.get("author", "")
    duration = details.get("lengthSeconds")
    fallback = Result(
        video_id=video_id,
        title=clean_title(raw_title),
        raw_title=raw_title,
        artists=[author] if author else [],
        duration_seconds=int(duration) if duration else None,
        thumbnail_url=_last_thumbnail(details),
    )
    try:
        for row in yt.search("%s %s" % (raw_title, author), filter="songs", limit=DEFAULT_LIMIT):
            if row.get("videoId") == video_id:
                enriched = _normalise(row)
                if enriched:
                    return enriched
    except Exception:
        # Enrichment is best-effort; the videoDetails fallback is enough.
        pass
    return fallback


def _last_thumbnail(details: dict) -> str:
    thumbnails = (details.get("thumbnail") or {}).get("thumbnails") or []
    return thumbnails[-1]["url"] if thumbnails else ""
