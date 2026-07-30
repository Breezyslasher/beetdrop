"""Inbox folder naming. Pure functions plus one directory probe.

Every grab lands in its own subdirectory named "<Artist> - <Title>". The
segment is sanitised: characters illegal on the target filesystem are
stripped, whitespace collapses, trailing dots and spaces are trimmed, the
result is capped at 200 bytes (bytes, not characters — the share may be
ext4 with a byte limit), and a segment may never resolve to "." or "..".
The empty-after-sanitisation case is handled explicitly.
"""

from __future__ import annotations

import re
from pathlib import Path

SEGMENT_MAX_BYTES = 200
FALLBACK_SEGMENT = "untitled"

# Illegal on NTFS/exFAT and unwise everywhere; slash and NUL are illegal on
# POSIX. Control characters are stripped wholesale.
_ILLEGAL_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_WS_RE = re.compile(r"\s+")


def sanitize_segment(segment: str, max_bytes: int = SEGMENT_MAX_BYTES) -> str:
    """Sanitise one path segment. Never returns empty, '.' or '..'."""
    cleaned = _ILLEGAL_RE.sub("", segment)
    cleaned = _WS_RE.sub(" ", cleaned).strip()
    cleaned = cleaned.rstrip(". ")
    cleaned = _truncate_utf8(cleaned, max_bytes)
    # Truncation can reintroduce a trailing dot or space.
    cleaned = cleaned.rstrip(". ")
    if not cleaned or cleaned in (".", ".."):
        return FALLBACK_SEGMENT
    return cleaned


def _truncate_utf8(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    # Do not cut a multi-byte sequence in half.
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def grab_folder_name(artist: str, title: str) -> str:
    """The inbox subdirectory name for one grab: "<Artist> - <Title>"."""
    artist = _WS_RE.sub(" ", artist).strip()
    title = _WS_RE.sub(" ", title).strip()
    if artist and title:
        combined = "%s - %s" % (artist, title)
    else:
        combined = artist or title
    return sanitize_segment(combined)


def unique_folder(inbox: Path, name: str) -> Path:
    """First non-existing "<name>", "<name> (2)", "<name> (3)"... in the
    inbox. Never overwrites."""
    candidate = inbox / name
    counter = 2
    while candidate.exists():
        suffixed = "%s (%d)" % (name, counter)
        # The suffix must survive the byte cap too.
        if len(suffixed.encode("utf-8")) > SEGMENT_MAX_BYTES:
            room = SEGMENT_MAX_BYTES - len((" (%d)" % counter).encode("utf-8"))
            suffixed = "%s (%d)" % (_truncate_utf8(name, room).rstrip(". "), counter)
        candidate = inbox / suffixed
        counter += 1
    return candidate
