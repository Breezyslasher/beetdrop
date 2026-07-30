"""Title cleaning. Pure functions.

Strip obvious noise from a YouTube title — Official Video, Official Audio,
Lyrics, Visualizer, HQ, and bare bracketed years — but never qualifiers
that change what the recording is: Live, Acoustic, Remix, Extended, Radio
Edit must survive to the card, because seeing them is how the user avoids
grabbing the wrong thing.

The raw title is kept in the record for debugging; only display and seed
tags use the cleaned one.
"""

from __future__ import annotations

import re

# Bracketed or parenthesised segments whose entire (normalised) content
# matches one of these is noise. Qualifiers like "Live", "Acoustic",
# "Remix", "Extended", "Radio Edit" are intentionally NOT here.
NOISE = {
    "official video",
    "official music video",
    "official audio",
    "official lyric video",
    "official lyrics video",
    "official visualizer",
    "official visualiser",
    "lyric video",
    "lyrics video",
    "lyrics",
    "visualizer",
    "visualiser",
    "hq",
    "hd",
    "4k",
    "official",
    "music video",
    "audio",
    "video",
}

_BRACKET_RE = re.compile(r"[(\[]([^()\[\]]*)[)\]]")
_YEAR_RE = re.compile(r"^(19|20)\d{2}$")
_WS_RE = re.compile(r"\s+")


def _is_noise(content: str) -> bool:
    norm = _WS_RE.sub(" ", content).strip().casefold()
    if not norm:
        return True
    if norm in NOISE:
        return True
    return bool(_YEAR_RE.match(norm))


def clean_title(raw: str) -> str:
    """Remove noise-only bracketed segments and collapse the leftovers."""

    def replace(match: re.Match) -> str:
        return " " if _is_noise(match.group(1)) else match.group(0)

    cleaned = _BRACKET_RE.sub(replace, raw)
    cleaned = _WS_RE.sub(" ", cleaned).strip()
    # Never clean a title into nothing.
    return cleaned if cleaned else raw.strip()
