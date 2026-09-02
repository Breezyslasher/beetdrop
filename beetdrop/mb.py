"""MusicBrainz access for library mode: serialised rate limiting plus an
SQLite response cache.

MusicBrainz allows one request per second per client and blocks
offenders, so every call goes through a hard serialising limiter (a
lock plus a monotonic-clock gate). Responses are cached with a long
TTL - metadata does not change hourly.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests

from . import APP_NAME, __version__

MB_ROOT = "https://musicbrainz.org/ws/2"
CAA_ROOT = "https://coverartarchive.org"
MIN_INTERVAL = 1.1
CACHE_TTL = 30 * 24 * 3600
TIMEOUT = 20

USER_AGENT = "%s/%s (+https://github.com/Breezyslasher/Youtube-Music-DL)" % (
    APP_NAME, __version__)


class MBError(RuntimeError):
    pass


def _lucene_escape(text: str) -> str:
    out = []
    for ch in text:
        if ch in '+-&|!(){}[]^"~*?:\\/':
            out.append("\\")
        out.append(ch)
    return "".join(out)


class MusicBrainzClient:
    def __init__(self, cache_path: Path):
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(cache_path), check_same_thread=False)
        self._db_lock = threading.Lock()
        with self._db_lock:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS mb_cache ("
                " key TEXT PRIMARY KEY, value TEXT NOT NULL,"
                " fetched_at REAL NOT NULL)"
            )
            self._db.commit()
        self._gate_lock = threading.Lock()
        self._next_ok = 0.0

    def close(self) -> None:
        with self._db_lock:
            self._db.close()

    # -- plumbing ----------------------------------------------------------

    def _gate(self) -> None:
        with self._gate_lock:
            now = time.monotonic()
            if now < self._next_ok:
                time.sleep(self._next_ok - now)
            self._next_ok = time.monotonic() + MIN_INTERVAL

    def _cached_get(self, url: str) -> dict:
        with self._db_lock:
            row = self._db.execute(
                "SELECT value, fetched_at FROM mb_cache WHERE key = ?", (url,)
            ).fetchone()
        if row and time.time() - row[1] <= CACHE_TTL:
            return json.loads(row[0])
        self._gate()
        try:
            response = requests.get(url, timeout=TIMEOUT,
                                    headers={"User-Agent": USER_AGENT})
        except requests.RequestException as exc:
            raise MBError("MusicBrainz unreachable: %s" % exc) from exc
        if not response.ok:
            raise MBError("MusicBrainz answered %d" % response.status_code)
        data = response.json()
        with self._db_lock:
            self._db.execute(
                "INSERT OR REPLACE INTO mb_cache (key, value, fetched_at)"
                " VALUES (?, ?, ?)",
                (url, json.dumps(data), time.time()),
            )
            self._db.commit()
        return data

    # -- API ---------------------------------------------------------------

    def search_recordings(self, title: str, artist: str, limit: int = 10) -> list[dict]:
        query = 'recording:"%s" AND artist:"%s"' % (
            _lucene_escape(title), _lucene_escape(artist))
        url = "%s/recording?query=%s&limit=%d&fmt=json" % (
            MB_ROOT, quote(query), limit)
        return self._cached_get(url).get("recordings", [])

    def search_releases(self, album: str, artist: str, limit: int = 10) -> list[dict]:
        query = 'release:"%s" AND artist:"%s"' % (
            _lucene_escape(album), _lucene_escape(artist))
        url = "%s/release?query=%s&limit=%d&fmt=json" % (
            MB_ROOT, quote(query), limit)
        return self._cached_get(url).get("releases", [])

    def get_release(self, release_mbid: str) -> dict:
        url = "%s/release/%s?inc=recordings+artist-credits+release-groups+media&fmt=json" % (
            MB_ROOT, release_mbid)
        return self._cached_get(url)

    # -- cover art (best effort, not rate limited by MB rules) -------------

    def fetch_cover(self, release_mbid: str = "", release_group_mbid: str = "",
                    thumbnail_url: str = "") -> Optional[tuple]:
        """(bytes, mime, source) from CAA front-500, falling back to the
        release group and finally the YouTube thumbnail."""
        candidates = []
        if release_mbid:
            candidates.append(("%s/release/%s/front-500" % (CAA_ROOT, release_mbid),
                               "caa-release"))
        if release_group_mbid:
            candidates.append(("%s/release-group/%s/front-500" % (CAA_ROOT, release_group_mbid),
                               "caa-release-group"))
        if thumbnail_url:
            candidates.append((thumbnail_url, "yt-thumbnail"))
        for url, source in candidates:
            try:
                response = requests.get(url, timeout=TIMEOUT, allow_redirects=True,
                                        headers={"User-Agent": USER_AGENT})
            except requests.RequestException:
                continue
            if not response.ok:  # a 404 here is normal, not an error
                continue
            mime = response.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
            if not mime.startswith("image/"):
                mime = "image/jpeg"
            return response.content, mime, source
        return None
