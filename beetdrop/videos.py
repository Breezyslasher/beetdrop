"""Music-video filing, Kodi-style.

Layout: {root}/{Artist}/{Artist} - {Title}.mp4 with a matching
{Artist} - {Title}.nfo sidecar and a {Artist} - {Title}-poster.jpg - the
names Kodi's Music Videos scraper reads directly, so a scan needs no
network lookup. The video root is kept separate from the audio library so
Kodi's music and music-video sources never overlap.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

from .fulltags import FullTags
from .paths import sanitize_segment

VIDEO_EXT = "mp4"


def video_basename(tags: FullTags) -> str:
    """"{Artist} - {Title}" as one filesystem-safe segment."""
    artist = tags.artist or tags.album_artist or "Unknown Artist"
    title = tags.title or "untitled"
    return sanitize_segment("%s - %s" % (artist, title))


def video_dir(root: Path, tags: FullTags) -> Path:
    artist = tags.artist or tags.album_artist or "Unknown Artist"
    return root / sanitize_segment(artist)


def video_filename(tags: FullTags, ext: str = VIDEO_EXT) -> str:
    return "%s.%s" % (video_basename(tags), ext)


def write_nfo(video_path: Path, tags: FullTags,
              duration_seconds: Optional[int] = None) -> Path:
    """A Kodi <musicvideo> NFO next to the video, same basename. Atomic,
    and never overwrites an existing NFO."""
    target = video_path.with_suffix(".nfo")
    if target.exists():
        return target

    root = ET.Element("musicvideo")
    ET.SubElement(root, "title").text = tags.title or video_path.stem
    ET.SubElement(root, "artist").text = tags.artist or tags.album_artist or ""
    if tags.album:
        ET.SubElement(root, "album").text = tags.album
    if tags.year:
        ET.SubElement(root, "year").text = tags.year
        ET.SubElement(root, "premiered").text = tags.date
    if duration_seconds:
        # Kodi reads music-video runtime in whole minutes.
        ET.SubElement(root, "runtime").text = str(max(1, round(duration_seconds / 60)))
    ET.SubElement(root, "track").text = str(tags.track_number or 0)
    ET.SubElement(root, "userrating").text = "0"
    # Point Kodi at the sibling poster explicitly (it also auto-detects it).
    ET.SubElement(root, "thumb", {"aspect": "poster"}).text = (
        "%s-poster.jpg" % video_path.stem)

    ET.indent(root)
    xml = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + \
        ET.tostring(root, encoding="utf-8")

    temp = target.parent / (".%s.beetdrop-tmp" % target.name)
    try:
        temp.write_bytes(xml)
        os.replace(temp, target)
    finally:
        if temp.exists():
            temp.unlink()
    return target


def write_poster(video_path: Path, cover: bytes,
                 mime: str = "image/jpeg") -> Path:
    """A {stem}-poster.jpg/png next to the video - the artwork Kodi shows
    for a music video. Atomic, never overwrites."""
    ext = "png" if mime == "image/png" else "jpg"
    target = video_path.with_name("%s-poster.%s" % (video_path.stem, ext))
    if target.exists():
        return target
    temp = target.parent / (".%s.beetdrop-tmp" % target.name)
    try:
        temp.write_bytes(cover)
        os.replace(temp, target)
    finally:
        if temp.exists():
            temp.unlink()
    return target
