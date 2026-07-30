"""Seed tags: the minimal metadata hint that lets beets' autotagger match.

Write only title, artist, albumartist, album, and tracknumber when actually
known. Nothing else. No date, no genre, no MBIDs, no cover art — every one
of those is beets' job, and a wrong guess here actively degrades beets'
matching. Seed values are a hint, not an answer; beets will overwrite all
of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import mutagen
from mutagen.easyid3 import EasyID3
from mutagen.mp3 import MP3


@dataclass
class SeedTags:
    title: str
    artist: str  # joined artist names
    albumartist: str  # first artist
    album: str  # result album, or the title if YouTube Music gave none
    tracknumber: Optional[int] = None  # omit unless known — do not invent 1


def seed_from_result(result) -> SeedTags:
    """Build seed tags from a normalised search Result."""
    artists = result.artists or ["Unknown Artist"]
    return SeedTags(
        title=result.title,
        artist=", ".join(artists),
        albumartist=artists[0],
        album=result.album or result.title,
        tracknumber=None,
    )


def write_seed_tags(path: Path, seed: SeedTags) -> None:
    """Write the seed via mutagen's easy interface, which maps the same
    keys onto Vorbis comments, iTunes atoms, and ID3."""
    audio = mutagen.File(str(path), easy=True)
    if audio is None:
        raise ValueError("mutagen cannot open %s" % path)
    if isinstance(audio, MP3) and audio.tags is None:
        audio.tags = EasyID3()
    audio["title"] = [seed.title]
    audio["artist"] = [seed.artist]
    audio["albumartist"] = [seed.albumartist]
    audio["album"] = [seed.album]
    if seed.tracknumber:
        audio["tracknumber"] = [str(seed.tracknumber)]
    audio.save()


def verify_audio(path: Path) -> None:
    """The file must exist, be non-empty, and open as valid audio."""
    if not path.is_file():
        raise ValueError("produced file is missing: %s" % path)
    if path.stat().st_size == 0:
        raise ValueError("produced file is empty: %s" % path)
    if mutagen.File(str(path)) is None:
        raise ValueError("produced file is not recognisable audio: %s" % path)
