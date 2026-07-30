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

    @property
    def artist_display(self) -> str:
        return ", ".join(self.artists)


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
        duration_seconds=row.get("duration_seconds"),
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
