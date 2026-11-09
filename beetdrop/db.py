"""SQLite state: recent jobs and settings.

One connection guarded by a lock, safe to call from the event loop and
from download worker threads alike. Jobs persist across restarts so the
queue view can be rebuilt on page load.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

JOB_COLUMNS = (
    "id", "video_id", "kind", "title", "artist", "format", "bitrate",
    "stage", "progress", "error", "detail", "log", "failed_tracks",
    "inbox_path", "inbox_state", "created_at", "updated_at", "with_video",
)

# failed_tracks is a JSON list of {"n": track_number, "title", "reason"}
# for album jobs that finished with gaps; it is what "Retry failed
# tracks" re-attempts.

# kind is "track" or "album". For album jobs the video_id column carries
# the YouTube Music album browseId; detail carries per-track progress
# text ("track 3/12: Title") and, when some tracks fail, the delivered
# count.

# inbox_state (column name kept for database compatibility) records the
# filing outcome: "" (not finished), "filed" (verified and in the
# library), "unverified" (no MusicBrainz match; filed under _review).

# Settings the API may read and write. yt-dlp version is reported
# read-only by the settings endpoint and lives nowhere.
SETTING_KEYS = ("output_format", "bitrate", "password", "concurrency",
                "music_root", "lyrics", "mxm_token", "lyrics_provider",
                "video_root", "video_max_height",
                "apple_token", "apple_storefront", "word_lyrics",
                "plex_url", "plex_token", "plex_music_path")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS jobs ("
                " id TEXT PRIMARY KEY,"
                " video_id TEXT NOT NULL,"
                " kind TEXT NOT NULL DEFAULT 'track',"
                " title TEXT NOT NULL DEFAULT '',"
                " artist TEXT NOT NULL DEFAULT '',"
                " format TEXT NOT NULL,"
                " bitrate TEXT NOT NULL,"
                " stage TEXT NOT NULL DEFAULT 'queued',"
                " progress REAL NOT NULL DEFAULT 0,"
                " error TEXT NOT NULL DEFAULT '',"
                " detail TEXT NOT NULL DEFAULT '',"
                " log TEXT NOT NULL DEFAULT '',"
                " failed_tracks TEXT NOT NULL DEFAULT '',"
                " inbox_path TEXT NOT NULL DEFAULT '',"
                " inbox_state TEXT NOT NULL DEFAULT '',"
                " created_at REAL NOT NULL,"
                " updated_at REAL NOT NULL)"
            )
            existing = {row[1] for row in self._db.execute("PRAGMA table_info(jobs)")}
            for column, definition in (
                ("inbox_state", "TEXT NOT NULL DEFAULT ''"),
                ("kind", "TEXT NOT NULL DEFAULT 'track'"),
                ("detail", "TEXT NOT NULL DEFAULT ''"),
                ("log", "TEXT NOT NULL DEFAULT ''"),
                ("failed_tracks", "TEXT NOT NULL DEFAULT ''"),
                # A track grab that also fetches its music video and files
                # it beside the track for Plex.
                ("with_video", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in existing:
                    self._db.execute(
                        "ALTER TABLE jobs ADD COLUMN %s %s" % (column, definition)
                    )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS settings ("
                " key TEXT PRIMARY KEY,"
                " value TEXT NOT NULL)"
            )
            # A track whose candidates were all refused, kept for a person
            # to judge. Keyed by path so a rescan updates rather than
            # duplicates, and cleared once decided.
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS lyric_reviews ("
                " path TEXT PRIMARY KEY,"
                " artist TEXT NOT NULL DEFAULT '',"
                " title TEXT NOT NULL DEFAULT '',"
                " duration INTEGER NOT NULL DEFAULT 0,"
                " candidates TEXT NOT NULL DEFAULT '[]',"
                " created_at REAL NOT NULL)"
            )
            # A decision, which outranks anything matching would pick.
            # song_id empty means "leave this track alone".
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS lyric_choices ("
                " path TEXT PRIMARY KEY,"
                " song_id TEXT NOT NULL DEFAULT '',"
                " label TEXT NOT NULL DEFAULT '',"
                " created_at REAL NOT NULL)"
            )
            self._db.commit()

    # -- lyric review queue ---------------------------------------------

    def add_review(self, path: str, artist: str, title: str, duration: int,
                   candidates: list) -> None:
        """Record that every candidate for this track was refused."""
        with self._lock:
            self._db.execute(
                "INSERT INTO lyric_reviews"
                " (path, artist, title, duration, candidates, created_at)"
                " VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(path) DO UPDATE SET"
                " artist=excluded.artist, title=excluded.title,"
                " duration=excluded.duration, candidates=excluded.candidates",
                (path, artist, title, int(duration or 0),
                 json.dumps(candidates), time.time()))
            self._db.commit()

    def list_reviews(self, limit: int = 200) -> list:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM lyric_reviews ORDER BY created_at LIMIT ?",
                (limit,)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["candidates"] = json.loads(item["candidates"])
            except ValueError:
                item["candidates"] = []
            out.append(item)
        return out

    def count_reviews(self) -> int:
        with self._lock:
            return self._db.execute(
                "SELECT COUNT(*) FROM lyric_reviews").fetchone()[0]

    def drop_review(self, path: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM lyric_reviews WHERE path = ?", (path,))
            self._db.commit()

    def drop_missing_reviews(self) -> int:
        """Forget queued tracks whose file is no longer there.

        The queue is keyed by path and only ever cleared by deciding a
        track or by hand, so a file deleted or moved after being queued
        stayed in the list for good - asking for a decision about
        something that no longer exists.
        """
        import os

        with self._lock:
            rows = self._db.execute("SELECT path FROM lyric_reviews").fetchall()
        gone = [row["path"] for row in rows if not os.path.exists(row["path"])]
        if not gone:
            return 0
        with self._lock:
            self._db.executemany("DELETE FROM lyric_reviews WHERE path = ?",
                                 [(path,) for path in gone])
            self._db.commit()
        return len(gone)

    def clear_reviews(self) -> int:
        with self._lock:
            removed = self._db.execute("DELETE FROM lyric_reviews").rowcount
            self._db.commit()
        return removed

    # -- decisions --------------------------------------------------------

    def set_choice(self, path: str, song_id: str, label: str = "") -> None:
        """Remember a decision. It outranks matching from now on, so a
        rescan never quietly undoes it."""
        with self._lock:
            self._db.execute(
                "INSERT INTO lyric_choices (path, song_id, label, created_at)"
                " VALUES (?,?,?,?)"
                " ON CONFLICT(path) DO UPDATE SET"
                " song_id=excluded.song_id, label=excluded.label",
                (path, song_id, label, time.time()))
            self._db.commit()

    def get_choice(self, path: str):
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM lyric_choices WHERE path = ?", (path,)).fetchone()
        return dict(row) if row else None

    def all_choices(self) -> dict:
        with self._lock:
            rows = self._db.execute("SELECT * FROM lyric_choices").fetchall()
        return {row["path"]: dict(row) for row in rows}

    def drop_choice(self, path: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM lyric_choices WHERE path = ?", (path,))
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- jobs ----------------------------------------------------------------

    def create_job(self, video_id: str, fmt: str, bitrate: str, kind: str = "track",
                   with_video: bool = False) -> dict:
        job_id = uuid.uuid4().hex[:12]
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO jobs (id, video_id, kind, format, bitrate, stage,"
                " created_at, updated_at, with_video)"
                " VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?)",
                (job_id, video_id, kind, fmt, bitrate, now, now, int(bool(with_video))),
            )
            self._db.commit()
        return self.get_job(job_id)

    def update_job(self, job_id: str, **fields) -> Optional[dict]:
        allowed = {k: v for k, v in fields.items() if k in JOB_COLUMNS and k != "id"}
        if not allowed:
            return self.get_job(job_id)
        allowed["updated_at"] = time.time()
        assignments = ", ".join("%s = ?" % k for k in allowed)
        with self._lock:
            self._db.execute(
                "UPDATE jobs SET %s WHERE id = ?" % assignments,
                (*allowed.values(), job_id),
            )
            self._db.commit()
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> Optional[dict]:
        with self._lock:
            row = self._db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def list_jobs(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def interrupted_jobs(self) -> list[dict]:
        """Jobs that were mid-flight when the process died."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM jobs WHERE stage NOT IN ('done', 'failed', 'cancelled')"
            ).fetchall()
        return [dict(row) for row in rows]

    def find_duplicate(self, video_id: str, kind: str) -> Optional[dict]:
        """Most recent job for the same id: an active one, else the most
        recent successful one. Used to warn before grabbing twice."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM jobs WHERE video_id = ? AND kind = ?"
                " AND stage NOT IN ('failed', 'cancelled')"
                " ORDER BY created_at DESC LIMIT 1",
                (video_id, kind),
            ).fetchone()
        return dict(row) if row else None

    def prune(self, keep_jobs: int, keep_days: int) -> int:
        """Delete terminal jobs that are BOTH beyond the newest keep_jobs
        and older than keep_days. Active jobs are never pruned."""
        cutoff = time.time() - keep_days * 86400
        with self._lock:
            result = self._db.execute(
                "DELETE FROM jobs WHERE stage IN ('done', 'failed', 'cancelled')"
                " AND created_at < ?"
                " AND id NOT IN (SELECT id FROM jobs ORDER BY created_at DESC LIMIT ?)",
                (cutoff, keep_jobs),
            )
            self._db.commit()
        return result.rowcount

    def count_failed_since(self, seconds: float) -> int:
        """Failed jobs in the trailing window - a wave of these usually
        means yt-dlp needs updating."""
        cutoff = time.time() - seconds
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM jobs WHERE stage = 'failed' AND updated_at > ?",
                (cutoff,),
            ).fetchone()
        return int(row[0])

    # -- settings ------------------------------------------------------------

    def get_settings(self) -> dict:
        with self._lock:
            rows = self._db.execute("SELECT key, value FROM settings").fetchall()
        return {row["key"]: row["value"] for row in rows if row["key"] in SETTING_KEYS}

    def set_settings(self, values: dict) -> None:
        items = [(k, str(v)) for k, v in values.items() if k in SETTING_KEYS]
        if not items:
            return
        with self._lock:
            self._db.executemany(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", items
            )
            self._db.commit()
