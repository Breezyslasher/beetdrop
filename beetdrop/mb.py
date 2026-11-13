"""MusicBrainz access for library mode: serialised rate limiting plus an
SQLite response cache.

MusicBrainz allows one request per second per client and blocks
offenders, so every call goes through a hard serialising limiter (a
lock plus a monotonic-clock gate). When it refuses anyway (503), every
request backs off together and tries again. Responses are cached with a long
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
# A search that found nothing is asked again after an hour, not a month:
# music is added to MusicBrainz all the time, and its search index can
# take a while to catch up with a newly added release.
EMPTY_TTL = 3600
TIMEOUT = 20

USER_AGENT = "%s/%s (+https://github.com/Breezyslasher/Youtube-Music-DL)" % (
    APP_NAME, __version__)


class MBError(RuntimeError):
    pass


class MBBusy(MBError):
    """MusicBrainz is refusing for now - rate limited or overloaded -
    as distinct from saying no to the request itself."""


# MusicBrainz answers 503 to a client over its rate limit, and when the
# whole service is over capacity; the others are a proxy or load
# balancer in between having a bad moment. All pass if asked again
# later, so each is retried a few times with growing pauses - about a
# minute at most - before giving up.
BUSY = (429, 500, 502, 503, 504)
BACKOFF = (2.0, 5.0, 15.0, 30.0)
MAX_BACKOFF = 60.0


def _lucene_escape(text: str) -> str:
    out = []
    for ch in text:
        if ch in '+-&|!(){}[]^"~*?:\\/':
            out.append("\\")
        out.append(ch)
    return "".join(out)


def _found_nothing(data) -> bool:
    """A search answer with no results in it."""
    if not isinstance(data, dict):
        return False
    for key in ("recordings", "releases"):
        if key in data and "id" not in data:
            return not data[key]
    return False


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

    def _back_off(self, response, fallback: float) -> None:
        """Hold every request, not just this one, for as long as
        MusicBrainz asked: it limits by address, so another job asking
        in the meantime only extends the refusal."""
        wait = fallback
        try:
            wait = float(response.headers.get("Retry-After") or fallback)
        except ValueError:
            pass
        wait = max(MIN_INTERVAL, min(wait, MAX_BACKOFF))
        with self._gate_lock:
            self._next_ok = max(self._next_ok, time.monotonic() + wait)

    def _cached_get(self, url: str) -> dict:
        with self._db_lock:
            row = self._db.execute(
                "SELECT value, fetched_at FROM mb_cache WHERE key = ?", (url,)
            ).fetchone()
        if row:
            cached = json.loads(row[0])
            ttl = EMPTY_TTL if _found_nothing(cached) else CACHE_TTL
            if time.time() - row[1] <= ttl:
                return cached
        for attempt in range(len(BACKOFF) + 1):
            self._gate()
            try:
                response = requests.get(url, timeout=TIMEOUT,
                                        headers={"User-Agent": USER_AGENT})
            except requests.RequestException as exc:
                raise MBError("MusicBrainz unreachable: %s" % exc) from exc
            if response.status_code not in BUSY or attempt == len(BACKOFF):
                break
            self._back_off(response, BACKOFF[attempt])
        if response.status_code in BUSY:
            raise MBBusy("MusicBrainz is busy (answered %d)" % response.status_code)
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

    def search_recordings(self, title: str, artist: str, limit: int = 10,
                          album: str = "") -> list[dict]:
        query = 'recording:"%s"' % _lucene_escape(title)
        if artist.strip():
            query += ' AND artist:"%s"' % _lucene_escape(artist)
        if album:
            # Recordings that appear on a release of that name - the way
            # to tell the album cut from the single, live or compilation
            # take when the title and artist are the same.
            query += ' AND release:"%s"' % _lucene_escape(album)
        url = "%s/recording?query=%s&limit=%d&fmt=json" % (
            MB_ROOT, quote(query), limit)
        return self._cached_get(url).get("recordings", [])

    def search_recordings_loose(self, title: str, artist: str, limit: int = 10,
                                album: str = "") -> list[dict]:
        """search_recordings by words rather than exact phrases: each field
        matches on any of its words, and MusicBrainz ranks the results by
        how many and how well. For a title or album spelled a little
        differently there - punctuation, a "feat.", an edition - where the
        phrase search finds nothing at all."""
        def words(text: str) -> str:
            return " ".join(_lucene_escape(w) for w in text.split())

        parts = ["recording:(%s)" % words(title)]
        if artist.strip():
            parts.append("artist:(%s)" % words(artist))
        if album.strip():
            parts.append("release:(%s)" % words(album))
        query = " AND ".join(parts)
        url = "%s/recording?query=%s&limit=%d&fmt=json" % (
            MB_ROOT, quote(query), limit)
        return self._cached_get(url).get("recordings", [])

    def search_releases(self, album: str, artist: str, limit: int = 10) -> list[dict]:
        query = 'release:"%s"' % _lucene_escape(album)
        if artist.strip():
            query += ' AND artist:"%s"' % _lucene_escape(artist)
        url = "%s/release?query=%s&limit=%d&fmt=json" % (
            MB_ROOT, quote(query), limit)
        return self._cached_get(url).get("releases", [])

    def get_release(self, release_mbid: str) -> dict:
        url = "%s/release/%s?inc=recordings+artist-credits+release-groups+media&fmt=json" % (
            MB_ROOT, release_mbid)
        return self._cached_get(url)

    def get_recording(self, recording_mbid: str) -> dict:
        """One recording by its ID, with the releases it is on - shaped like
        a search result, so it can stand in for one."""
        url = ("%s/recording/%s?inc=artist-credits+releases+release-groups+media"
               "&fmt=json" % (MB_ROOT, recording_mbid))
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
