"""Configuration.

Phase 1 keeps this to environment variables with sane defaults; the
settings API arrives with the web layer in phase 2.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# FLAC and WAV are deliberately absent: YouTube Music's source ceiling is
# roughly 160 kbps Opus or 256 kbps AAC, so a lossless container would be a
# larger file carrying no additional information.
SUPPORTED_FORMATS = ("opus", "m4a", "mp3")


@dataclass
class Config:
    inbox: Path = field(default_factory=lambda: Path(os.environ.get("INBOX_PATH", "/inbox")))
    scratch_root: Path = field(default_factory=lambda: Path(os.environ.get("TRACKPULL_SCRATCH", "/tmp/trackpull")))
    output_format: str = field(default_factory=lambda: os.environ.get("TRACKPULL_FORMAT", "opus"))
    bitrate: str = field(default_factory=lambda: os.environ.get("TRACKPULL_BITRATE", "192"))
    cookies_file: str = field(default_factory=lambda: os.environ.get("TRACKPULL_COOKIES", ""))


def check_inbox(inbox: Path) -> None:
    """Fail loudly if the inbox does not exist or is not writable by the
    effective UID. A permissions error discovered after a download completes
    is a bad experience and an easily avoided one."""
    if not inbox.is_dir():
        raise SystemExit("inbox path does not exist or is not a directory: %s" % inbox)
    if not os.access(inbox, os.W_OK | os.X_OK):
        raise SystemExit("inbox path is not writable by uid %d: %s" % (os.geteuid(), inbox))
