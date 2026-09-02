"""Configuration.

Phase 1 keeps this to environment variables with sane defaults; the
settings API arrives with the web layer in phase 2.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

def _env(name: str, default: str) -> str:
    """BEETDROP_* wins; the legacy TRACKPULL_* name is still honored so
    existing deployments survive the rename."""
    return os.environ.get("BEETDROP_" + name,
                          os.environ.get("TRACKPULL_" + name, default))


# FLAC and WAV are deliberately absent: YouTube Music's source ceiling is
# roughly 160 kbps Opus or 256 kbps AAC, so a lossless container would be a
# larger file carrying no additional information.
SUPPORTED_FORMATS = ("opus", "m4a", "mp3")


class StorageError(RuntimeError):
    pass


InboxError = StorageError  # legacy alias


@dataclass
class Config:
    scratch_root: Path = field(default_factory=lambda: Path(_env("SCRATCH", "/tmp/beetdrop")))
    config_dir: Path = field(default_factory=lambda: Path(_env("CONFIG", "~/.config/beetdrop")).expanduser())
    output_format: str = field(default_factory=lambda: _env("FORMAT", "opus"))
    bitrate: str = field(default_factory=lambda: _env("BITRATE", "192"))
    cookies_file: str = field(default_factory=lambda: _env("COOKIES", ""))
    password: str = field(default_factory=lambda: _env("PASSWORD", ""))
    # Refuse grabs when the library filesystem has less than this much free.
    # Running out of disk mid-album otherwise surfaces as a confusing
    # ffmpeg/yt-dlp error after the download already happened.
    min_free_mb: int = field(default_factory=lambda: int(_env("MIN_FREE_MB", "512")))
    # Randomized pause between album tracks ("min-max" seconds, or a
    # single number). Back-to-back downloads look bot-like to YouTube's
    # throttling heuristics.
    track_delay: str = field(default_factory=lambda: _env("TRACK_DELAY", "2-5"))
    # Concurrent download workers (1-4). Applied at startup.
    concurrency: int = field(default_factory=lambda: int(_env("CONCURRENCY", "2")))
    # Job history retention: terminal jobs beyond both limits are pruned.
    keep_jobs: int = field(default_factory=lambda: int(_env("KEEP_JOBS", "200")))
    keep_days: int = field(default_factory=lambda: int(_env("KEEP_DAYS", "30")))
    # The music library Beetdrop tags and files into.
    music_root: Path = field(default_factory=lambda: Path(os.environ.get("MUSIC_PATH", "/music")))
    # Fetch synced (timed) lyrics from LRCLIB and write a .lrc sidecar.
    lyrics_enabled: bool = field(default_factory=lambda: _env("LYRICS", "1") not in ("0", "false", "no", ""))
    # Primary synced-lyrics source: "lrclib" (default, free, no token) or
    # "musixmatch" (best coverage, needs a rotating usertoken). Whichever
    # is not primary is used as the fallback.
    lyrics_provider: str = field(default_factory=lambda: _env("LYRICS_PROVIDER", "lrclib"))
    musixmatch_token: str = field(default_factory=lambda: _env("MXM_TOKEN", ""))

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
        new = self.config_dir / "beetdrop.sqlite3"
        legacy = self.config_dir / "trackpull.sqlite3"
        if legacy.is_file() and not new.exists():
            # Pre-rename state: carry the job history and settings over.
            try:
                os.replace(legacy, new)
            except OSError:
                return legacy
        return new


def storage_free_mb(root: Path) -> int:
    try:
        return shutil.disk_usage(root).free // (1024 * 1024)
    except OSError:
        return 0


def storage_problem(root: Path, min_free_mb: int = 0) -> str:
    """Empty string when the library root is usable, else the reason."""
    if not root.is_dir():
        return "music library path does not exist or is not a directory: %s" % root
    if not os.access(root, os.W_OK | os.X_OK):
        return "music library path is not writable by uid %d: %s" % (os.geteuid(), root)
    if min_free_mb > 0:
        free = storage_free_mb(root)
        if free < min_free_mb:
            return "library filesystem has %d MB free, below the %d MB minimum" % (free, min_free_mb)
    return ""


def check_storage(root: Path, min_free_mb: int = 0) -> None:
    """Fail loudly if the library is missing, unwritable by the effective
    UID, or nearly out of disk. Any of these discovered after a download
    completes is a bad experience and an easily avoided one."""
    problem = storage_problem(root, min_free_mb)
    if problem:
        raise InboxError(problem)
