"""What is actually on disk, for the Library and Stats screens.

Both are read-only views over a walk of the library tree. No cache: at a
real library size - 5,794 tracks, 4,295 sidecars - the walk plus a read
of every .lrc measures 0.08s warm and under half a second cold, which is
not worth a second copy of the truth that can go stale.

Embedded tags are the exception and are deliberately not read here. A
mutagen open per file is the one part that is not free, and nothing on
these screens needs it: album and artist come from the path Beetdrop
itself filed the track into, and the rest is stat() and sidecar text.
"""

from __future__ import annotations

import base64
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple, Optional

from .lyrics import (has_backwards_word_timing, has_crowded_word_timing,
                     has_word_timing, looks_synthetic, lyric_source_label)

AUDIO_EXTS = (".opus", ".ogg", ".mp3", ".m4a", ".flac")
VIDEO_EXTS = (".mp4", ".mkv", ".webm")
REVIEW_DIR = "_review"
COVER_NAMES = ("cover.jpg", "cover.png", "cover.jpeg")
# "Album (2013)" -> name and year, which is how album_dir writes them.
_YEAR = re.compile(r"^(.*?)\s*\((\d{4})\)\s*$")
# "01 - Title", "1-04 - Title": the leading number filing gives a track.
_TRACK_NO = re.compile(r"^(?:(\d{1,2})-)?(\d{1,3})\s*-\s*\S")


def album_id(root: Path, folder: Path) -> str:
    """A URL-safe id for an album folder.

    The relative path, encoded - reversible, so nothing has to keep an
    index of ids just to look one back up.
    """
    try:
        relative = folder.relative_to(root)
    except ValueError:
        relative = Path(folder.name)
    return base64.urlsafe_b64encode(str(relative).encode("utf-8")).decode("ascii").rstrip("=")


def album_path(root: Path, ident: str) -> Optional[Path]:
    """The folder an id names, or None if it points outside the library."""
    try:
        padded = ident + "=" * (-len(ident) % 4)
        relative = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except Exception:
        return None
    candidate = (root / relative).resolve()
    base = root.resolve()
    if candidate != base and base not in candidate.parents:
        return None
    return candidate


class Sidecar(NamedTuple):
    """Everything one sidecar has to say, from a single read.

    It used to take two walks of the library and two reads of every .lrc
    to get this: once here for the state, and again in bad_timing_count
    for the word timing. Both parse the same text.
    """

    state: str = "none"        # word | line | junk | none
    source: str = ""           # who wrote it, per its [re:] tag
    backwards: bool = False    # word tags run backwards mid-line
    crowded: bool = False      # word tags packed too tight to be real

    @property
    def timing(self) -> str:
        """The one word for what is wrong with the timing, if anything."""
        if self.backwards:
            return "backwards"
        return "crowded" if self.crowded else ""


def read_sidecar(sidecar: Path) -> Sidecar:
    """What one track's .lrc is, who wrote it, and whether it is sound.

    The source comes from the [re:] tag Beetdrop stamps on the way out -
    or from another tool's, when it named itself the same way. A file
    with no tag reports "" rather than a guess.
    """
    if not sidecar.is_file():
        return Sidecar()
    try:
        text = sidecar.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return Sidecar()
    source = lyric_source_label(text)
    if looks_synthetic(text):
        return Sidecar("junk", source)
    if not has_word_timing(text):
        return Sidecar("line", source)
    return Sidecar("word", source, has_backwards_word_timing(text),
                   has_crowded_word_timing(text))


def lyric_state(sidecar: Path) -> str:
    """"word", "line", "junk" or "none" for one track's sidecar."""
    return read_sidecar(sidecar).state


def _split_year(name: str):
    found = _YEAR.match(name)
    return (found.group(1), found.group(2)) if found else (name, "")


def _track_number(name: str) -> int:
    found = _TRACK_NO.match(name)
    return int(found.group(2)) if found else 0


@dataclass
class Album:
    id: str = ""
    album: str = ""
    artist: str = ""
    year: str = ""
    path: str = ""
    track_count: int = 0
    # Highest track number the filenames carry. Derived from numbering,
    # not from MusicBrainz: the release total would mean reading a
    # release id out of every file's tags, which is the one expensive
    # part of this. A gap in the numbering is what "incomplete" means
    # here, and it is what an interrupted album grab actually leaves.
    expected_count: int = 0
    lyrics: dict = field(default_factory=dict)
    # Which source wrote each sidecar, by count. "" is a file with no
    # tag: written before the tag existed, or by something else.
    lyric_sources: dict = field(default_factory=dict)
    format: str = ""
    size_bytes: int = 0
    added_at: float = 0.0
    verified: bool = True
    has_cover: bool = False
    # Word-level sidecars whose timing is not sound. Counted here so the
    # Stats totals cost nothing beyond the walk already being done.
    backwards_timing: int = 0
    crowded_timing: int = 0

    @property
    def incomplete(self) -> bool:
        return self.expected_count > self.track_count


def scan_albums(root: Path) -> list:
    """Every folder holding audio, as one album row each."""
    folders: dict = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in AUDIO_EXTS:
            continue
        folders.setdefault(path.parent, []).append(path)

    albums = []
    for folder, tracks in folders.items():
        try:
            relative = folder.relative_to(root)
        except ValueError:
            continue
        parts = relative.parts
        in_review = bool(parts) and parts[0] == REVIEW_DIR
        album_name, year = _split_year(folder.name)
        artist = folder.parent.name if len(parts) > 1 else ""
        if in_review:
            # _review/{Artist} - {Title}/ is one track in its own folder.
            artist, _, album_name = folder.name.partition(" - ")
            album_name = album_name or folder.name
            year = ""

        counts = Counter()
        sources = Counter()
        newest = 0.0
        size = 0
        numbers = []
        backwards = crowded = 0
        for track in tracks:
            found = read_sidecar(track.with_suffix(".lrc"))
            counts[found.state] += 1
            if found.state != "none":
                sources[found.source] += 1
            backwards += found.backwards
            crowded += found.crowded
            try:
                info = track.stat()
                size += info.st_size
                newest = max(newest, info.st_mtime)
            except OSError:
                pass
            numbers.append(_track_number(track.name))

        albums.append(Album(
            id=album_id(root, folder),
            album=album_name, artist=artist, year=year, path=str(folder),
            track_count=len(tracks),
            expected_count=max(numbers) if numbers else 0,
            lyrics={state: counts.get(state, 0)
                    for state in ("word", "line", "junk", "none")},
            lyric_sources=dict(sources),
            format=Counter(t.suffix.lstrip(".").lower()
                           for t in tracks).most_common(1)[0][0],
            size_bytes=size, added_at=newest, verified=not in_review,
            has_cover=any((folder / name).is_file() for name in COVER_NAMES),
            backwards_timing=backwards, crowded_timing=crowded,
        ))
    return albums


FILTERS = ("missing_lyrics", "line_only", "unverified", "incomplete", "junk",
           "backwards", "crowded")


def matches_filter(album: Album, wanted: str) -> bool:
    if not wanted:
        return True
    if wanted.startswith("format:"):
        return album.format == wanted.split(":", 1)[1].lower()
    if wanted == "missing_lyrics":
        return album.lyrics.get("none", 0) > 0
    if wanted == "line_only":
        return album.lyrics.get("line", 0) > 0
    if wanted == "unverified":
        return not album.verified
    if wanted == "incomplete":
        return album.incomplete
    if wanted == "junk":
        return album.lyrics.get("junk", 0) > 0
    if wanted == "backwards":
        return album.backwards_timing > 0
    if wanted == "crowded":
        return album.crowded_timing > 0
    return True


SORTS = ("added", "az", "size", "year")


def sort_albums(albums: list, how: str) -> list:
    if how == "az":
        return sorted(albums, key=lambda a: (a.artist.lower(), a.album.lower()))
    if how == "size":
        return sorted(albums, key=lambda a: -a.size_bytes)
    if how == "year":
        return sorted(albums, key=lambda a: (a.year or "0000"), reverse=True)
    return sorted(albums, key=lambda a: -a.added_at)


def album_tracks(folder: Path) -> list:
    """One row per track in an album, with its own lyrics state."""
    rows = []
    for path in sorted(folder.iterdir()):
        if not (path.is_file() and path.suffix.lower() in AUDIO_EXTS):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        found = read_sidecar(path.with_suffix(".lrc"))
        rows.append({
            "name": path.name, "path": str(path),
            "number": _track_number(path.name),
            "lyrics": found.state,
            "lyrics_source": found.source,
            "lyrics_timing": found.timing,
            "size_bytes": size,
            "format": path.suffix.lstrip(".").lower(),
        })
    return sorted(rows, key=lambda row: (row["number"], row["name"]))


def count_videos(root: Path) -> int:
    return sum(1 for path in root.rglob("*")
               if path.is_file() and path.suffix.lower() in VIDEO_EXTS)


def count_companion_videos(music_root: Path, video_root: Path) -> int:
    """Music videos filed beside their tracks for Plex.

    They live in album folders under the music root rather than in the
    video library, so counting the video library alone missed them. The
    video library is skipped here even when it sits inside the music
    root, which is the default, so nothing is counted twice.
    """
    from .companion import PLEX_VIDEO_SUFFIX

    try:
        skip = video_root.resolve()
    except OSError:
        skip = None
    total = 0
    for path in music_root.rglob("*%s.mp4" % PLEX_VIDEO_SUFFIX):
        if not path.is_file():
            continue
        if skip is not None and skip in path.resolve().parents:
            continue
        total += 1
    return total


def summarise(albums: list) -> dict:
    """The library totals the Stats screen leads with."""
    lyrics = Counter()
    sources = Counter()
    formats: dict = {}
    artists = set()
    tracks = size = incomplete = review = 0
    backwards = crowded = 0
    for album in albums:
        tracks += album.track_count
        size += album.size_bytes
        if album.artist:
            artists.add(album.artist.lower())
        if album.incomplete:
            incomplete += 1
        if not album.verified:
            review += album.track_count
        for state, count in album.lyrics.items():
            lyrics[state] += count
        for name, count in (album.lyric_sources or {}).items():
            sources[name or "untagged"] += count
        backwards += album.backwards_timing
        crowded += album.crowded_timing
        entry = formats.setdefault(album.format, {"ext": album.format,
                                                  "count": 0, "bytes": 0})
        entry["count"] += album.track_count
        entry["bytes"] += album.size_bytes
    return {
        "tracks": tracks,
        "albums": len(albums),
        "artists": len(artists),
        "incomplete_albums": incomplete,
        "review_count": review,
        "bytes_used": size,
        "verified_pct": (100.0 * (tracks - review) / tracks) if tracks else 100.0,
        "lyrics": {
            **{state: lyrics.get(state, 0)
               for state in ("word", "line", "junk", "none")},
            # Which source wrote each sidecar. "untagged" is every file
            # written before Beetdrop stamped one, so it shrinks as
            # passes rewrite them rather than meaning "unknown source".
            # Untagged last whatever its size: it is a backlog, not a
            # source, and reading it as the biggest provider would be
            # exactly the wrong conclusion.
            "bad_timing": backwards,
            # Word tags packed too tight to be real - what a forced
            # aligner leaves on a line with more words than its window
            # holds. Passes every other check, so nothing found these.
            "crowded": crowded,
            "by_source": [{"source": name, "count": count}
                          for name, count in sorted(
                              sources.most_common(),
                              key=lambda row: (row[0] == "untagged", -row[1]))],
        },
        "formats": sorted(formats.values(), key=lambda row: -row["count"]),
    }


def grab_history(jobs: list, days: int = 14) -> dict:
    """Reliability and per-day activity, from the jobs already recorded.

    No new bookkeeping: the jobs table is the record. It is pruned -
    KEEP_JOBS newest and KEEP_DAYS old, both must be exceeded - so a busy
    fortnight can fall off the front. That is reported rather than
    hidden: a chart that quietly loses its early days reads as a drop in
    activity that did not happen.
    """
    import time

    now = time.time()
    cutoff = now - days * 86400
    grabs = [job for job in jobs
             if job.get("kind") in ("track", "album", "musicvideo")]
    recent = [job for job in grabs if (job.get("created_at") or 0) >= cutoff]

    done = sum(1 for job in recent if job.get("stage") == "done")
    failed = sum(1 for job in recent if job.get("stage") == "failed")
    settled = done + failed

    reasons = Counter()
    for job in recent:
        if job.get("stage") != "failed":
            continue
        # The first line is the reason; the rest is a traceback or a URL
        # and would make every failure look unique.
        text = (job.get("error") or "unknown").strip().splitlines()[0]
        reasons[text[:80]] += 1

    buckets = {}
    for offset in range(days):
        day = time.strftime("%Y-%m-%d",
                            time.localtime(now - (days - 1 - offset) * 86400))
        buckets[day] = {"date": day, "audio": 0, "video": 0, "failed": 0}
    for job in recent:
        day = time.strftime("%Y-%m-%d",
                            time.localtime(job.get("created_at") or now))
        bucket = buckets.get(day)
        if bucket is None:
            continue
        if job.get("stage") == "failed":
            bucket["failed"] += 1
        elif job.get("kind") == "musicvideo":
            bucket["video"] += 1
        else:
            bucket["audio"] += 1

    oldest = min((job.get("created_at") or now) for job in grabs) if grabs else now
    return {
        "success_pct": (100.0 * done / settled) if settled else 100.0,
        "failed": failed,
        "failure_reasons": [{"reason": reason, "count": count}
                            for reason, count in reasons.most_common(6)],
        "activity": list(buckets.values()),
        # True when the record does not reach back the whole window, so
        # the early bars are missing history rather than a quiet period.
        "history_capped": bool(grabs) and oldest > cutoff,
        "history_days": days,
    }
