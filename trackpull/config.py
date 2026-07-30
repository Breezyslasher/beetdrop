"""Configuration.

Phase 1 keeps this to environment variables with sane defaults; the
settings API arrives with the web layer in phase 2.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

# FLAC and WAV are deliberately absent: YouTube Music's source ceiling is
# roughly 160 kbps Opus or 256 kbps AAC, so a lossless container would be a
# larger file carrying no additional information.
SUPPORTED_FORMATS = ("opus", "m4a", "mp3")


class InboxError(RuntimeError):
    pass


@dataclass
class Config:
    inbox: Path = field(default_factory=lambda: Path(os.environ.get("INBOX_PATH", "/inbox")))
    scratch_root: Path = field(default_factory=lambda: Path(os.environ.get("TRACKPULL_SCRATCH", "/tmp/trackpull")))
    config_dir: Path = field(default_factory=lambda: Path(os.environ.get("TRACKPULL_CONFIG", "~/.config/trackpull")).expanduser())
    output_format: str = field(default_factory=lambda: os.environ.get("TRACKPULL_FORMAT", "opus"))
    bitrate: str = field(default_factory=lambda: os.environ.get("TRACKPULL_BITRATE", "192"))
    cookies_file: str = field(default_factory=lambda: os.environ.get("TRACKPULL_COOKIES", ""))
    password: str = field(default_factory=lambda: os.environ.get("TRACKPULL_PASSWORD", ""))
    # Refuse grabs when the inbox filesystem has less than this much free.
    # Running out of disk mid-album otherwise surfaces as a confusing
    # ffmpeg/yt-dlp error after the download already happened.
    min_free_mb: int = field(default_factory=lambda: int(os.environ.get("TRACKPULL_MIN_FREE_MB", "512")))
    # Randomized pause between album tracks ("min-max" seconds, or a
    # single number). Back-to-back downloads look bot-like to YouTube's
    # throttling heuristics.
    track_delay: str = field(default_factory=lambda: os.environ.get("TRACKPULL_TRACK_DELAY", "2-5"))
    # Concurrent download workers (1-4). Applied at startup.
    concurrency: int = field(default_factory=lambda: int(os.environ.get("TRACKPULL_CONCURRENCY", "2")))
    # Job history retention: terminal jobs beyond both limits are pruned.
    keep_jobs: int = field(default_factory=lambda: int(os.environ.get("TRACKPULL_KEEP_JOBS", "200")))
    keep_days: int = field(default_factory=lambda: int(os.environ.get("TRACKPULL_KEEP_DAYS", "30")))

    def track_delay_range(self) -> tuple:
        try:
            parts = self.track_delay.split("-", 1)
            low = float(parts[0])
            high = float(parts[1]) if len(parts) > 1 else low
        except (ValueError, AttributeError):
            return (2.0, 5.0)
        if high < low:
            low, high = high, low
        return (max(0.0, low), max(0.0, high))

    @property
    def db_path(self) -> Path:
        return self.config_dir / "trackpull.sqlite3"


def inbox_free_mb(inbox: Path) -> int:
    try:
        return shutil.disk_usage(inbox).free // (1024 * 1024)
    except OSError:
        return 0


def inbox_problem(inbox: Path, min_free_mb: int = 0) -> str:
    """Empty string when the inbox is usable, otherwise the reason it is not."""
    if not inbox.is_dir():
        return "inbox path does not exist or is not a directory: %s" % inbox
    if not os.access(inbox, os.W_OK | os.X_OK):
        return "inbox path is not writable by uid %d: %s" % (os.geteuid(), inbox)
    if min_free_mb > 0:
        free = inbox_free_mb(inbox)
        if free < min_free_mb:
            return "inbox filesystem has %d MB free, below the %d MB minimum" % (free, min_free_mb)
    return ""


def check_inbox(inbox: Path, min_free_mb: int = 0) -> None:
    """Fail loudly if the inbox is missing, unwritable by the effective
    UID, or nearly out of disk. Any of these discovered after a download
    completes is a bad experience and an easily avoided one."""
    problem = inbox_problem(inbox, min_free_mb)
    if problem:
        raise InboxError(problem)
