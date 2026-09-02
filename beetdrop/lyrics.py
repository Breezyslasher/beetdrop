"""Synced (timed) lyrics from LRCLIB.

Timed only: LRCLIB returns both synced (LRC with [mm:ss.xx] timestamps)
and plain lyrics; we use the synced form and skip anything that only
has plain text. Best effort - no lyrics is normal, never an error - and
written as a .lrc sidecar next to the audio file, which is what Plex,
Navidrome, and most players read for synced lyrics.
"""

from __future__ import annotations

from typing import Optional

import requests

from .mb import USER_AGENT

LRCLIB_GET = "https://lrclib.net/api/get"
TIMEOUT = 10


def fetch_synced_lyrics(artist: str, title: str, album: str = "",
                        duration_seconds: Optional[int] = None) -> Optional[str]:
    """The LRC text for this track, or None when no *synced* lyrics exist.

    Duration is what lets LRCLIB return the correctly-timed version, so it
    is passed whenever known.
    """
    if not artist or not title:
        return None
    params = {"artist_name": artist, "track_name": title}
    if album:
        params["album_name"] = album
    if duration_seconds:
        params["duration"] = str(int(duration_seconds))
    try:
        response = requests.get(LRCLIB_GET, params=params, timeout=TIMEOUT,
                                headers={"User-Agent": USER_AGENT})
    except requests.RequestException:
        return None
    if not response.ok:  # 404 = no lyrics known; normal
        return None
    try:
        data = response.json()
    except ValueError:
        return None
    synced = (data.get("syncedLyrics") or "").strip()
    return synced or None  # plain-only results are intentionally skipped
