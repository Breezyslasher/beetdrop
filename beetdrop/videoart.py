"""Embed the artwork already sitting beside music videos into the videos.

Grabs embed a video's YouTube thumbnail as its cover art now. Videos
filed before that - by Beetdrop or another tool - often have the same
picture as a separate image beside them instead:

    02 Go!!! (From Naruto)-video.mp4
    02 Go!!! (From Naruto)-video-thumb.jpg     or -thumb.jpg, -poster.jpg, .jpg

Players that read embedded art (Kodi, Infuse, VLC, file browsers) do not
look for those. This copies the image into the video, once: a video that
already has embedded art is left alone, and the image file is kept,
since what reads it still does.

Rewriting a video's metadata moves data inside a file that may be
hundreds of megabytes, so it is done on a copy beside it, which then
replaces the original in one step. An interrupted pass leaves either
the old file or the new one, never half of each.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

from .companion import _image_kind

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
    from mutagen.mp4 import MP4
    tags = MP4(str(video)).tags
    return bool(tags and tags.get("covr"))


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


def backfill_video_art(roots: Iterable[Path],
                       on_progress: Callable[[float], None] = _noop,
                       on_detail: Callable[[str], None] = _noop) -> ArtBackfill:
    result = ArtBackfill()
    videos = iter_videos(roots)
    result.total = len(videos)
    for index, video in enumerate(videos):
        on_progress(index / result.total * 100.0 if result.total else 100.0)
        try:
            if has_cover(video):
                result.already += 1
                continue
        except Exception:
            result.failed += 1
            on_detail("could not read %s" % video.name)
            continue
        image = sidecar_image(video)
        if image is None:
            result.no_image += 1
            continue
        try:
            data = image.read_bytes()
        except OSError:
            result.failed += 1
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
        except Exception as exc:
            result.failed += 1
            on_detail("could not write %s (%s)" % (video.name, str(exc)[:120]))
    on_progress(100.0)
    return result
