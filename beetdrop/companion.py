"""A track's music video, filed beside the track for Plex.

Plex reads music videos in a music library as extras of the artist and
album, and links one to a specific track when, in the same folder, its
name starts with the track's exact filename and ends in a type suffix -
"-video" for a music video:

    Artist/Album (2013)/03 - Song.opus
    Artist/Album (2013)/03 - Song-video.mp4

The suffix is what makes it a music video; a bare "03 - Song.mp4" is
not read as one. Versions 0.65 to 0.67 named the video for the title
alone ("Song-video.mp4"), which Plex does not link to "03 - Song";
rename_title_named() renames those once.

This is a different home from the Videos tab, which files a video into a
separate library Kodi-style, because Kodi treats music and music videos
as two library types and Plex does not. The two coexist: a companion is
named for the track it belongs to, not for Kodi.

The naming follows Plex's support article ("Adding Local Artist and
Music Videos") as quoted, not a live server, so it lives in one
function. If Plex ever wants it spelled differently, that is the only
line that changes.

A companion is best effort, always. The audio is what was asked for;
failing to find or fetch a video never fails the grab, it only says so.
"""

from __future__ import annotations

import re
import shutil
import uuid
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Optional

from .fulltags import FullTags, verify_audio, write_full_tags
from .matching import base_title, normalize, normalize_artist, significant_qualifiers
from .paths import sanitize_segment
from .search import Result

PLEX_VIDEO_SUFFIX = "-video"
VIDEO_EXT = "mp4"

# A music video is routinely longer than the track - a cold open, a
# spoken intro, a dance break, credits. It is almost never shorter by
# much; that is a radio edit or a different song.
LONGER_BY_AT_MOST = 150
SHORTER_BY_AT_MOST = 10

# Uploads that are on YouTube as a "video" without being one. An "Official
# Audio" upload is a still image over the track; a lyric video is a real
# Plex extra type, but not the one asked for here.
_NOT_A_MUSIC_VIDEO = re.compile(
    r"\b(lyrics?|audio|visuali[sz]er|reaction|karaoke|tutorial|"
    r"slowed|sped up|nightcore|8d|fan made|fanmade)\b")
_OFFICIAL = re.compile(r"\bofficial\b")


def plex_video_filename(audio_name: str, ext: str = VIDEO_EXT) -> str:
    """The name Plex reads as the music video of the track filed as
    `audio_name`: that filename without its extension, "-video", and the
    video's own extension. "03 - Song.opus" -> "03 - Song-video.mp4"."""
    return "%s%s.%s" % (Path(audio_name).stem, PLEX_VIDEO_SUFFIX, ext)


def companion_path(audio_path: Path) -> Path:
    return audio_path.parent / plex_video_filename(audio_path.name)


def _title_named(audio_path: Path, title: str) -> Path:
    """Where 0.65 to 0.67 put the video: named for the title alone."""
    return audio_path.parent / ("%s%s.%s" % (sanitize_segment(title or "untitled"),
                                             PLEX_VIDEO_SUFFIX, VIDEO_EXT))


@dataclass
class Pick:
    result: Optional[Result]
    reason: str = ""


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


_DASH = re.compile(r"\s+[-\u2013\u2014|]\s+")


def _title_ratio(candidate_title: str, want_title: str) -> float:
    """How well a video's title names the track.

    A YouTube video is usually titled "Artist - Title (Official Video)",
    sometimes "Title - Artist", where YouTube Music's own entries give the
    bare title. Each side of the dash is tried, so the artist in the
    title neither helps nor hurts.
    """
    whole = base_title(candidate_title)
    parts = [whole] + [base_title(part) for part in _DASH.split(candidate_title)]
    return max(_ratio(part, want_title) for part in parts if part) if any(parts) else 0.0


def pick_music_video(candidates: list, title: str, artist: str,
                     duration: Optional[int]) -> Pick:
    """The candidate that is this track's music video, or why none is.

    Matched against what was actually downloaded, not against
    MusicBrainz: if the match was wrong, the audio is still the song
    YouTube served, and its video is that song's video.

    Strict on identity and loose on length. A wrong video beside a track
    is worse than none - Plex shows it as that song - so anything that is
    a different recording (live, remix, acoustic) or not a video at all
    (official audio, lyric video, visualizer) is refused outright.
    """
    want_title = base_title(title)
    want_artist = normalize_artist(artist)
    want_qualifiers = significant_qualifiers(title)
    if not want_title:
        return Pick(None, "the track has no usable title to search with")

    best, best_score, refused = None, -1.0, []
    for candidate in candidates:
        raw = normalize(candidate.raw_title or candidate.title)
        if _NOT_A_MUSIC_VIDEO.search(raw):
            refused.append("not a music video")
            continue
        if significant_qualifiers(candidate.raw_title or candidate.title) != want_qualifiers:
            refused.append("a different recording (live, remix...)")
            continue
        title_ratio = _title_ratio(candidate.title, want_title)
        if title_ratio < 0.85:
            refused.append("a different title")
            continue
        names = [normalize_artist(name) for name in candidate.artists] or [""]
        artist_ratio = max(_ratio(name, want_artist) for name in names)
        if want_artist and artist_ratio < 0.8 and not any(
                want_artist in name or (name and name in want_artist)
                for name in names):
            refused.append("a different artist")
            continue
        score = 50.0 * title_ratio + 30.0 * artist_ratio
        if duration and candidate.duration_seconds:
            difference = candidate.duration_seconds - duration
            if difference < -SHORTER_BY_AT_MOST or difference > LONGER_BY_AT_MOST:
                refused.append("the wrong length for this track")
                continue
            score += 15.0 * (1 - abs(difference) / LONGER_BY_AT_MOST)
        if _OFFICIAL.search(raw):
            score += 5.0
        if score > best_score:
            best, best_score = candidate, score

    if best is not None:
        return Pick(best)
    if not candidates:
        return Pick(None, "YouTube Music has no music video for it")
    common = max(set(refused), key=refused.count) if refused else "no match"
    return Pick(None, "no music video matched (closest was %s)" % common)


@dataclass
class Companion:
    path: Optional[Path]
    detail: str


def _noop(*args) -> None:
    pass


def grab_companion(config, audio_path: Path, tags: FullTags, result: Result,
                   search: Callable, download: Callable,
                   on_progress: Callable[[float], None] = _noop,
                   logger=None) -> Companion:
    """Find, download and file this track's music video beside it.

    `search` and `download` are passed in so the job layer and the tests
    decide what talks to YouTube - this module only decides what to keep.
    Never raises for an ordinary miss; a companion is a bonus on a grab
    that has already succeeded.
    """
    target = companion_path(audio_path)
    if target.exists():
        # place_file would file "03 - Song-video (2).mp4", which Plex
        # reads as nothing at all. One video per track; the first stays.
        return Companion(target, "a music video was already beside it")
    old = _title_named(audio_path, tags.title or result.title)
    if old != target and old.is_file():
        # Grabbed by an earlier version under a name Plex does not link.
        old.replace(target)
        return Companion(target, "renamed the music video beside it so Plex "
                                 "links it to the track")

    query = "%s %s" % (result.primary_artist, result.title)
    try:
        candidates = search(query)
    except Exception as exc:
        return Companion(None, "could not search for a music video (%s)" % exc)
    pick = pick_music_video(candidates, result.title, result.primary_artist,
                            result.duration_seconds)
    if pick.result is None:
        return Companion(None, pick.reason)

    scratch = config.scratch_root / ("video-" + uuid.uuid4().hex[:12])
    try:
        def progress_hook(data: dict) -> None:
            if data.get("status") != "downloading":
                return
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            done = data.get("downloaded_bytes")
            if total and done is not None:
                on_progress(min(100.0, done / total * 100.0))

        video = download(pick.result.video_id, scratch,
                         max_height=config.video_max_height,
                         cookies_file=config.cookies_file,
                         progress_hook=progress_hook, logger=logger)
        verify_audio(video)
        try:
            # The same tags as the track, so anything reading the file
            # rather than its name still knows what it is.
            write_full_tags(video, tags)
        except Exception:
            pass
        temp = target.parent / (".%s.beetdrop-tmp" % target.name)
        try:
            shutil.copyfile(video, temp)
            temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)
        return Companion(target, "music video filed beside it (%s)"
                         % (pick.result.raw_title or pick.result.title))
    except Exception as exc:
        return Companion(None, "found a music video but could not fetch it (%s)"
                         % str(exc)[:200])
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def carry_companion(old_audio: Path, old_title: str,
                    new_audio: Path) -> Optional[Path]:
    """Move a track's music video along when the track is re-filed.

    A wrong-match repair re-tags the audio and moves it. The video was
    chosen to match the audio that was downloaded, not MusicBrainz's
    opinion of it, so it is still the right video - but it is named for
    the old file in the old folder, where Plex would link it to nothing.
    `old_title` finds a video an earlier version named for the title.
    """
    source = companion_path(old_audio)
    if not source.is_file():
        source = _title_named(old_audio, old_title)
        if not source.is_file():
            return None
    target = companion_path(new_audio)
    if target == source:
        return target
    if target.exists():
        # Something is already filed there; leave this one where it was
        # rather than overwrite a video the person may have chosen.
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    return target


_NUMBERED = re.compile(r"^(?:\d+-)?\d+ - (?P<title>.+)$")


def rename_title_named(music_root: Path) -> int:
    """Rename videos 0.65 to 0.67 named for the title ("Song-video.mp4")
    to the name Plex links ("03 - Song-video.mp4"). Only where exactly
    one numbered track in the same folder has that title, so a guess is
    never made; returns how many were renamed."""
    from .backfill import AUDIO_EXTS
    renamed = 0
    suffix = PLEX_VIDEO_SUFFIX + "." + VIDEO_EXT
    for video in music_root.rglob("*" + suffix):
        prefix = video.name[:-len(suffix)]
        folder = video.parent
        try:
            audio = [p for p in folder.iterdir()
                     if p.is_file() and p.suffix.lower() in AUDIO_EXTS]
        except OSError:
            continue
        if any(p.stem == prefix for p in audio):
            continue    # already named for its track
        owners = [p for p in audio
                  if (m := _NUMBERED.match(p.stem)) and m.group("title") == prefix]
        if len(owners) != 1:
            continue
        target = companion_path(owners[0])
        if target.exists():
            continue
        try:
            video.replace(target)
            renamed += 1
        except OSError:
            continue
    return renamed
