"""Embed the artwork already sitting beside music videos into the videos.

Grabs embed a video's YouTube thumbnail as its cover art now. Videos
filed before that - by Beetdrop or another tool - often have the same
picture as a separate image beside them instead:

    02 Go!!! (From Naruto)-video.mp4
    02 Go!!! (From Naruto)-video-thumb.jpg     or -thumb.jpg, -poster.jpg, .jpg

Players that read embedded art (Kodi, Infuse, VLC, file browsers) do not
look for those. This copies the image into the video, once - or, with
no image but a YouTube link in its tags (yt-dlp's --embed-metadata
writes one), fetches that video's own thumbnail: a video that
already has embedded art is left alone, and the image file is kept,
since what reads it still does.

Rewriting a video's metadata moves data inside a file that may be
hundreds of megabytes, so it is done on a copy beside it, which then
replaces the original in one step. An interrupted pass leaves either
the old file or the new one, never half of each.
"""

from __future__ import annotations

import errno
import os
import re
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from .companion import _image_kind, fetch_thumbnail

VIDEO_EXTS = (".mp4", ".m4v")
IMAGE_EXTS = (".jpg", ".jpeg", ".png")
# Plex's extra types, stripped to find an image named for the track
# rather than for the video file.
TYPE_SUFFIXES = ("-video", "-lyrics", "-live", "-behindthescenes", "-concert",
                 "-interview", "-featurette", "-other")
# Kodi's and Jellyfin's names first: "-thumb" is usually a frame of the
# video itself, which is what this picture should be.
IMAGE_SUFFIXES = ("-thumb", "-poster", "")


@dataclass
class ArtBackfill:
    total: int = 0
    embedded: int = 0
    already: int = 0       # had embedded art; left alone
    no_image: int = 0      # nothing beside it to embed
    unusable: int = 0      # an image MP4 cannot hold (WebP, say)
    failed: int = 0        # could not be read or written
    by_prefix: int = 0     # of embedded: image found by a shortened name
    from_youtube: int = 0  # of embedded: thumbnail fetched by the link in its tags
    no_thumbnail: int = 0  # had a YouTube link, but no thumbnail could be fetched
    not_mp4: int = 0       # WebM or Matroska named .mp4: cannot hold MP4 art
    converted: int = 0     # of those, remuxed into a real MP4 (when asked)
    from_search: int = 0   # of embedded: found by searching YouTube for its name
    searched: int = 0      # searched for, when asked
    no_match: int = 0      # searched, and nothing was sure enough to use
    search_stopped: str = ""   # why searching was abandoned part-way, if it was
    # A few searches that found nothing certain, with the closest result
    # and what ruled it out - to judge whether the rules are too strict.
    unmatched: list = field(default_factory=list)
    # Why the failures failed, counted, so the card can say "permission
    # denied" rather than only how many.
    reasons: Counter = field(default_factory=Counter)
    # Why a WebM/MKV was not converted, when converting was asked for.
    left: Counter = field(default_factory=Counter)
    # A few videos with no image, and the images in the same folder that
    # nothing claimed - enough to see what naming was not understood.
    missing: list = field(default_factory=list)

    def report(self) -> str:
        lines = []
        if self.reasons:
            lines.append("Could not be read or written:")
            lines += ["  %d - %s" % (n, why) for why, n in self.reasons.most_common()]
        if self.left:
            lines.append("WebM/MKV left as they were:")
            lines += ["  %d - %s" % (n, why) for why, n in self.left.most_common()]
        if self.search_stopped:
            lines.append("Searching YouTube stopped: %s" % self.search_stopped)
        if self.unmatched:
            lines.append("Searched YouTube, nothing certain enough to use:")
            for name, closest in self.unmatched:
                lines.append("  %s" % name)
                lines.append("    closest: %s" % closest)
        if self.missing:
            lines.append("Examples with no image found:")
            for name, images in self.missing:
                lines.append("  %s" % name)
                lines.append("    images in that folder: %s" % (
                    ", ".join(images) if images else "none"))
        return "\n".join(lines)


MISSING_EXAMPLES = 8


def _why(exc: BaseException, writing: bool) -> str:
    """A failure in words a person can act on."""
    if isinstance(exc, PermissionError):
        return ("permission denied - Beetdrop cannot write in that folder"
                if writing else "permission denied - Beetdrop cannot read it")
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return "the disk is full"
    if isinstance(exc, OSError):
        return "%s" % (exc.strerror or exc.__class__.__name__)
    name = exc.__class__.__name__
    if not writing or "MP4" in name or "Mutagen" in name:
        return "not a readable MP4 (%s)" % name
    return name


def sidecar_image(video: Path) -> Optional[Path]:
    """The image beside `video` that is its picture, if any."""
    stems = [video.stem]
    for suffix in TYPE_SUFFIXES:
        if video.stem.endswith(suffix) and len(video.stem) > len(suffix):
            stems.append(video.stem[:-len(suffix)])
            break
    for stem in stems:
        for name_suffix in IMAGE_SUFFIXES:
            for ext in IMAGE_EXTS:
                for spelled in (ext, ext.upper()):
                    candidate = video.with_name(stem + name_suffix + spelled)
                    if candidate.is_file():
                        return candidate
    return None


def has_cover(video: Path) -> bool:
    return _inspect(video)[0]


def video_length(video: Path) -> float:
    from mutagen.mp4 import MP4
    return float(MP4(str(video)).info.length or 0)


# yt-dlp's --embed-metadata writes the page URL into the comment tag
# (\xa9cmt); a link to the video in any tag will do. Its default filename
# ends in " [<id>]".
_YT_LINK = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|embed/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])")
_YT_IN_NAME = re.compile(r"\[([A-Za-z0-9_-]{11})\]$")


def youtube_id(video: Path, tags=None) -> str:
    """The YouTube video this file came from, when it says so."""
    for values in (tags or {}).values():
        for value in values if isinstance(values, list) else [values]:
            if isinstance(value, str):
                found = _YT_LINK.search(value)
                if found:
                    return found.group(1)
    found = _YT_IN_NAME.search(video.stem)
    return found.group(1) if found else ""


def _inspect(video: Path) -> tuple:
    """(has embedded art, YouTube id from its tags or name)."""
    from mutagen.mp4 import MP4
    tags = MP4(str(video)).tags
    return bool(tags and tags.get("covr")), youtube_id(video, tags)


_MATROSKA = b"\x1a\x45\xdf\xa3"


def _is_matroska(video: Path) -> bool:
    try:
        with open(video, "rb") as handle:
            return handle.read(4) == _MATROSKA
    except OSError:
        return False


# What MP4 players widely play. Vorbis, say, goes into an MP4 without
# complaint and then plays in few of them - such a file is better left
# as the WebM it is.
MP4_VIDEO = {"h264", "hevc", "vp9", "av1", "mpeg4"}
MP4_AUDIO = {"aac", "mp3", "opus", "ac3", "eac3", "flac", "alac"}
REMUX_TIMEOUT = 900


class RemuxRefused(Exception):
    """The file was left as it is, and this says why."""


def _probe(path: Path) -> dict:
    import json
    import subprocess
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_entries", "stream=codec_type,codec_name:format=duration", str(path)],
        capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        raise RemuxRefused("ffprobe could not read it")
    return json.loads(out.stdout or "{}")


def remux_to_mp4(video: Path) -> None:
    """Rewrite a WebM/MKV named .mp4 as a real MP4, same name, same
    streams: video and audio are copied untouched (nothing re-encoded),
    the tags come along - a YouTube link in them included. Subtitle and
    attachment streams are dropped; MP4 has no place for most of them.

    Checked before it replaces anything: the codecs must be ones MP4
    players play, and the result must be as long as the original.
    """
    import subprocess

    info = _probe(video)
    streams = info.get("streams") or []
    kinds = {(s.get("codec_type"), s.get("codec_name")) for s in streams}
    videos = {name for kind, name in kinds if kind == "video"}
    audios = {name for kind, name in kinds if kind == "audio"}
    if not videos:
        raise RemuxRefused("no video stream")
    odd = (videos - MP4_VIDEO) | (audios - MP4_AUDIO)
    if odd:
        raise RemuxRefused("%s does not play well in MP4" % ", ".join(sorted(odd)))

    temp = video.with_name(".%s.beetdrop-remux.mp4" % video.stem)
    try:
        run = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(video),
             "-map", "0:v", "-map", "0:a?", "-c", "copy", "-map_metadata", "0",
             "-movflags", "+faststart", "-f", "mp4", str(temp)],
            capture_output=True, text=True, timeout=REMUX_TIMEOUT)
        if run.returncode != 0:
            raise RemuxRefused("ffmpeg: %s" % (run.stderr.strip().splitlines() or ["failed"])[-1][:150])
        before = float((info.get("format") or {}).get("duration") or 0)
        after = float((_probe(temp).get("format") or {}).get("duration") or 0)
        if before and abs(before - after) > max(1.0, before * 0.01):
            raise RemuxRefused("the MP4 came out %.0fs long, not %.0fs" % (after, before))
        shutil.copymode(video, temp)
        os.replace(temp, video)
    finally:
        temp.unlink(missing_ok=True)


def embed_cover(video: Path, data: bytes, mime: str) -> None:
    """Embed `data` as `video`'s cover art, via a copy that replaces it."""
    from mutagen.mp4 import MP4, MP4Cover

    temp = video.with_name(".%s.beetdrop-tmp" % video.name)
    try:
        shutil.copy2(video, temp)
        tagged = MP4(str(temp))
        if tagged.tags is None:
            tagged.add_tags()
        fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
        tagged.tags["covr"] = [MP4Cover(data, imageformat=fmt)]
        tagged.save()
        os.replace(temp, video)
    finally:
        temp.unlink(missing_ok=True)


def iter_videos(roots: Iterable[Path]) -> list:
    """Every MP4 video under the roots, once each, oldest names first.
    Hidden files are skipped - that includes Beetdrop's own temp copies."""
    seen, found = set(), []
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if (path.suffix.lower() in VIDEO_EXTS and not path.name.startswith(".")
                    and path.is_file()):
                key = path.resolve()
                if key not in seen:
                    seen.add(key)
                    found.append(path)
    return sorted(found)


def _noop(*args) -> None:
    pass


_TRACK_NUMBER = re.compile(r"^\s*(?:\d+-)?\d{1,3}\s*[-.]\s+")
# A downloaded video is the same stream YouTube serves, so its length
# agrees to the second; this allows for rounding and container padding.
LENGTH_SLACK = 2.5
TITLE_AT_LEAST = 0.9
SEARCH_PAUSE = 1.0
GIVE_UP_AFTER = 5     # searches failing in a row: YouTube is refusing


def search_query(video: Path) -> str:
    """What to search YouTube for: the name, without the "07 - " a
    library adds or a Plex type suffix."""
    stem = _video_bases(video)[-1]
    return _TRACK_NUMBER.sub("", stem).strip()


def match_on_youtube(query: str, length: float, candidates: list) -> tuple:
    """(the result to take, or None; a line on the closest one).

    Both must hold, or nothing is taken: the title says the same thing
    once brackets and punctuation are set aside - checked against the
    video's title alone and with its artist in front, since a filename
    is usually "Artist - Title (Official Video)" - and the length is the
    file's own, to the couple of seconds. Of several that pass, the
    closest in length.
    """
    from .companion import _ratio
    from .matching import base_title

    want = base_title(query)
    best, best_gap, closest, closest_score = None, None, "", None
    for candidate in candidates:
        names = [candidate.raw_title or candidate.title, candidate.title]
        for artist in candidate.artists[:2]:
            names += ["%s - %s" % (artist, n) for n in names[:2]]
        title_score = max(_ratio(base_title(n), want) for n in names if n)
        gap = (abs(candidate.duration_seconds - length)
               if candidate.duration_seconds and length else None)
        if title_score >= TITLE_AT_LEAST and gap is not None and gap <= LENGTH_SLACK:
            if best_gap is None or gap < best_gap:
                best, best_gap = candidate, gap
            continue
        score = title_score - (min(gap, 60) / 60 if gap is not None else 1)
        if closest_score is None or score > closest_score:
            closest_score = score
            closest = '"%s" - title %.0f%% alike, %s' % (
                candidate.raw_title or candidate.title, title_score * 100,
                "length %+.0fs" % (candidate.duration_seconds - length)
                if gap is not None else "length unknown")
    return best, closest or "no results"


def _key(text: str) -> str:
    return re.sub(r"[^0-9a-z]", "", text.casefold())


def _image_base(image: Path) -> str:
    stem = image.stem
    for suffix in IMAGE_SUFFIXES:
        if suffix and stem.endswith(suffix):
            return stem[:-len(suffix)]
    return stem


def _video_bases(video: Path) -> list:
    bases = [video.stem]
    for suffix in TYPE_SUFFIXES:
        if video.stem.endswith(suffix) and len(video.stem) > len(suffix):
            bases.append(video.stem[:-len(suffix)])
            break
    return bases


def _by_prefix(videos: list, images: list) -> dict:
    """Pair videos with images whose names were cut short - one name the
    start of the other, as a tool that limits filename length leaves
    them. Only one-to-one pairs count: an image that could belong to two
    videos, or a video that could take two images, is left unpaired
    rather than guessed. Eight letters or digits at least."""
    def fits(video, image):
        image_key = _key(_image_base(image))
        for base in _video_bases(video):
            video_key = _key(base)
            if min(len(image_key), len(video_key)) >= 8 and (
                    video_key.startswith(image_key) or image_key.startswith(video_key)):
                return True
        return False

    pairs = {}
    for video in videos:
        options = [image for image in images if fits(video, image)]
        if len(options) == 1 and sum(fits(v, options[0]) for v in videos) == 1:
            pairs[video] = options[0]
    return pairs


def backfill_video_art(roots: Iterable[Path],
                       on_progress: Callable[[float], None] = _noop,
                       on_detail: Callable[[str], None] = _noop,
                       fetch_art: Callable = fetch_thumbnail,
                       convert: bool = False,
                       search: Optional[Callable] = None,
                       pause: float = SEARCH_PAUSE,
                       sleep: Callable[[float], None] = None) -> ArtBackfill:
    """`search`, when given, is a YouTube video search (a query in, a list
    of search.Result out), tried for videos with no image beside them and
    no link in their tags - one search per `pause` seconds."""
    import time
    sleep = sleep or time.sleep
    failing_in_a_row = 0
    result = ArtBackfill()
    videos = iter_videos(roots)
    result.total = len(videos)
    folders = defaultdict(list)
    for video in videos:
        folders[video.parent].append(video)

    done = 0
    for folder, here in folders.items():
        try:
            images = [p for p in folder.iterdir()
                      if p.suffix.lower() in IMAGE_EXTS and p.is_file()]
        except OSError:
            images = []
        # Images some video in this folder is named for; never handed to
        # another video by a looser match.
        named_for = {image for video in here
                     for image in [sidecar_image(video)] if image}
        named_for |= {image for image in images
                      if any(_key(_image_base(image)) == _key(base)
                             for video in here for base in _video_bases(video))}

        wanting, linked = [], {}
        for video in here:
            done += 1
            on_progress((done - 1) / result.total * 100.0)
            try:
                covered, linked[video] = _inspect(video)
                if covered:
                    result.already += 1
                    continue
            except Exception as exc:
                if _is_matroska(video):
                    # Not damaged: a WebM or MKV someone named .mp4. It
                    # plays, but has no place for MP4 cover art - unless,
                    # when asked, it is remuxed into the MP4 it claims to be.
                    result.not_mp4 += 1
                    if not convert:
                        continue
                    on_detail("converting %s to MP4" % video.name)
                    try:
                        remux_to_mp4(video)
                        covered, linked[video] = _inspect(video)
                    except Exception as remux_exc:
                        why = (str(remux_exc) if isinstance(remux_exc, RemuxRefused)
                               else _why(remux_exc, writing=True))
                        result.left[why] += 1
                        on_detail("left %s as it is (%s)" % (video.name, why))
                        continue
                    result.converted += 1
                    if not covered:
                        wanting.append(video)
                    else:
                        result.already += 1
                    continue
                result.failed += 1
                result.reasons[_why(exc, writing=False)] += 1
                on_detail("could not read %s (%s)" % (video.name, _why(exc, False)))
                continue
            wanting.append(video)

        spare = [image for image in images if image not in named_for]
        loose = _by_prefix([v for v in wanting if sidecar_image(v) is None], spare)
        for video in wanting:
            image = sidecar_image(video) or loose.get(video)
            if image is None and linked.get(video):
                # Nothing beside it, but it says which YouTube video it
                # is: that video's own thumbnail, as new grabs embed.
                try:
                    art = fetch_art(linked[video], "")
                except Exception:
                    art = None
                if not art:
                    result.no_thumbnail += 1
                    continue
                data, mime = art
                on_detail("embedding YouTube's thumbnail for %s" % video.name)
                try:
                    embed_cover(video, data, mime)
                    result.embedded += 1
                    result.from_youtube += 1
                except Exception as exc:
                    result.failed += 1
                    result.reasons[_why(exc, writing=True)] += 1
                continue
            if image is None and search is not None and not result.search_stopped:
                query = search_query(video)
                try:
                    length = video_length(video)
                except Exception:
                    length = 0.0
                if query and length:
                    if result.searched:
                        sleep(pause)
                    result.searched += 1
                    on_detail("searching YouTube for %s" % query)
                    try:
                        candidates = search(query)
                        failing_in_a_row = 0
                    except Exception as exc:
                        candidates = None
                        failing_in_a_row += 1
                        if failing_in_a_row >= GIVE_UP_AFTER:
                            result.search_stopped = (
                                "%d searches in a row failed (%s) - YouTube may be "
                                "refusing; run it again later" % (
                                    failing_in_a_row, str(exc)[:120]))
                    if candidates is not None:
                        found, closest = match_on_youtube(query, length, candidates)
                        if found is None:
                            result.no_match += 1
                            if len(result.unmatched) < MISSING_EXAMPLES:
                                result.unmatched.append((video.name, closest))
                            continue
                        try:
                            art = fetch_art(found.video_id, found.thumbnail_url)
                        except Exception:
                            art = None
                        if not art:
                            result.no_thumbnail += 1
                            continue
                        on_detail("embedding the thumbnail of %s" % (
                            found.raw_title or found.title))
                        try:
                            embed_cover(video, *art)
                            result.embedded += 1
                            result.from_search += 1
                        except Exception as exc:
                            result.failed += 1
                            result.reasons[_why(exc, writing=True)] += 1
                        continue
            if image is None:
                result.no_image += 1
                if len(result.missing) < MISSING_EXAMPLES:
                    result.missing.append((
                        video.name, sorted(p.name for p in spare)[:6]))
                continue
            try:
                data = image.read_bytes()
            except OSError as exc:
                result.failed += 1
                result.reasons[_why(exc, writing=False)] += 1
                continue
            mime = _image_kind(data)
            if not mime:
                result.unusable += 1
                on_detail("%s is not a JPEG or PNG" % image.name)
                continue
            on_detail("embedding %s" % image.name)
            try:
                embed_cover(video, data, mime)
                result.embedded += 1
                if video in loose:
                    result.by_prefix += 1
            except Exception as exc:
                result.failed += 1
                result.reasons[_why(exc, writing=True)] += 1
                on_detail("could not write %s (%s)" % (video.name, _why(exc, True)))
    on_progress(100.0)
    return result
