"""Download via yt-dlp as a library.

format: "bestaudio/best", with FFmpegExtractAudio as the postprocessor.
yt-dlp's ExtractAudio stream-copies when the source codec already matches
the target (opus-to-opus, aac-to-m4a), so a re-encode only happens when it
must — which in practice means the mp3 compatibility option.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path
from typing import Callable, Optional

import yt_dlp


class LogCollector:
    """Ring buffer that satisfies yt-dlp's logger interface. Keeps the
    last N lines so a failed job can show what led up to the failure."""

    def __init__(self, limit: int = 80):
        self._lines: deque = deque(maxlen=limit)

    def add(self, message) -> None:
        text = str(message).strip()
        if text:
            self._lines.append(text[:300])

    # yt-dlp logger interface
    def debug(self, message) -> None:
        self.add(message)

    def info(self, message) -> None:
        self.add(message)

    def warning(self, message) -> None:
        self.add("WARNING: %s" % message)

    def error(self, message) -> None:
        self.add("ERROR: %s" % message)

    def text(self) -> str:
        return "\n".join(self._lines)

# Steer the selector toward the container that lets ExtractAudio copy
# instead of transcode.
_FORMAT_SELECTORS = {
    "opus": "bestaudio[acodec^=opus]/bestaudio/best",
    "m4a": "bestaudio[ext=m4a]/bestaudio/best",
    "mp3": "bestaudio/best",
}


class DownloadError(RuntimeError):
    pass


def download_audio(
    video_id: str,
    scratch_dir: Path,
    fmt: str = "opus",
    bitrate: str = "192",
    cookies_file: str = "",
    progress_hook: Optional[Callable[[dict], None]] = None,
    postprocessor_hook: Optional[Callable[[dict], None]] = None,
    logger: Optional[LogCollector] = None,
) -> Path:
    """Download bestaudio into scratch_dir, extract to fmt, and return the
    produced file path."""
    if fmt not in _FORMAT_SELECTORS:
        raise ValueError("unsupported format: %s" % fmt)
    scratch_dir.mkdir(parents=True, exist_ok=True)

    postprocessor = {"key": "FFmpegExtractAudio", "preferredcodec": fmt}
    if fmt == "mp3":
        postprocessor["preferredquality"] = bitrate

    ydl_opts = {
        "format": _FORMAT_SELECTORS[fmt],
        "outtmpl": str(scratch_dir / "%(id)s.%(ext)s"),
        "postprocessors": [postprocessor],
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    if cookies_file:
        ydl_opts["cookiefile"] = cookies_file
    if progress_hook:
        ydl_opts["progress_hooks"] = [progress_hook]
    if postprocessor_hook:
        ydl_opts["postprocessor_hooks"] = [postprocessor_hook]
    if logger is not None:
        ydl_opts["logger"] = logger

    url = "https://music.youtube.com/watch?v=%s" % video_id
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except yt_dlp.utils.DownloadError as exc:
        raise DownloadError(str(exc)) from exc

    produced = scratch_dir / ("%s.%s" % (video_id, fmt))
    if not produced.is_file():
        leftovers = [
            p for p in sorted(scratch_dir.glob("%s.*" % video_id))
            if p.suffix not in (".part", ".ytdl", ".webm")
        ]
        if not leftovers:
            raise DownloadError("yt-dlp produced no output for %s" % video_id)
        produced = leftovers[0]
    return produced


def ytdlp_version() -> str:
    return yt_dlp.version.__version__
