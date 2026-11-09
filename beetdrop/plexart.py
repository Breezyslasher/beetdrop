"""Show music videos in Plex with the artwork embedded in them.

Plex does not read a music video's embedded cover art: it draws the
thumbnail of a music video extra from a frame. What it does take is an
image uploaded through its API - POST /library/metadata/{id}/thumb with
the image as the body, which only the server's owner may do (Plex Media
Server API 1.2.2, "Set an item's artwork, theme, etc").

So: walk the artists of the music library, ask Plex for each one's
extras - that is where it lists music videos, both those beside a track
and those beside the artist - map each extra's file back to the file
here, and upload the art embedded in it.

Plex keeps every image uploaded to an item, so the same picture is
never sent twice: what was sent is remembered by item and by a hash of
the image, and only a changed or new picture goes.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import plex
from .plex import PlexError, _call, _contains

VIDEO_EXTS = (".mp4", ".m4v")
ARTIST = 8                 # Plex's metadata type number for an artist
STATE_FILE = "plex_artwork_sent.json"
GIVE_UP_AFTER = 5          # uploads failing in a row: Plex will not take them


@dataclass
class PlexArt:
    artists: int = 0
    videos: int = 0        # music video extras Plex listed
    sent: int = 0
    unchanged: int = 0     # this picture was sent before
    no_art: int = 0        # the file has no embedded artwork
    not_here: int = 0      # Plex's file is not under the music folder here
    failed: int = 0
    reasons: Counter = field(default_factory=Counter)
    stopped: str = ""      # why it gave up part-way, if it did


def _container(response) -> dict:
    try:
        return (response.json() or {}).get("MediaContainer") or {}
    except ValueError as exc:
        raise PlexError("Plex sent something that is not JSON") from exc


def _files(item: dict) -> list:
    return [part.get("file", "") for media in item.get("Media") or []
            for part in media.get("Part") or [] if part.get("file")]


def local_path(plex_file: str, base: str, music_root: Path) -> Optional[Path]:
    """The file here that Plex knows as `plex_file`, given that Plex sees
    the music folder at `base`."""
    if not base or not _contains(base, plex_file):
        return None
    relative = plex_file[len(base.rstrip("/")):].lstrip("/")
    return music_root / relative if relative else None


def embedded_art(video: Path) -> Optional[tuple]:
    """(bytes, MIME type) of the cover art embedded in an MP4, or None."""
    from mutagen.mp4 import MP4, MP4Cover

    tags = MP4(str(video)).tags
    covers = (tags or {}).get("covr") or []
    if not covers:
        return None
    cover = covers[0]
    mime = ("image/png" if getattr(cover, "imageformat", None) == MP4Cover.FORMAT_PNG
            else "image/jpeg")
    return bytes(cover), mime


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, state: dict) -> None:
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(state))
    temp.replace(path)


def _noop(*args) -> None:
    pass


def send_artwork(config, on_progress: Callable[[float], None] = _noop,
                 on_detail: Callable[[str], None] = _noop) -> PlexArt:
    """Upload each music video's embedded artwork as its thumbnail in
    Plex. Raises PlexError when Plex cannot be used at all."""
    if not (config.plex_url and config.plex_token):
        raise PlexError("Plex is not set up - sign in to Plex in Settings first")
    url, token = config.plex_url, config.plex_token
    all_sections = plex.sections(url, token, fresh=True)
    base, _how = plex.mapping(config, all_sections)
    if not base:
        raise PlexError("Beetdrop cannot tell where Plex sees the music folder - "
                        "set it in Settings")
    chosen = plex.chosen_section(config, all_sections)
    libraries = [chosen] if chosen else [
        s for s in plex.sections_for(all_sections, base) if s["type"] == "artist"]
    if not libraries:
        raise PlexError("none of Plex's music libraries holds %s" % base)

    state_path = config.config_dir / STATE_FILE
    state = _load_state(state_path)
    sent_here = state.setdefault(url.rstrip("/"), {})
    result = PlexArt()

    artist_keys = []
    for library in libraries:
        listing = _container(_call("GET", url, token,
                                   "/library/sections/%s/all" % library["key"],
                                   params={"type": ARTIST}))
        artist_keys += [str(a.get("ratingKey")) for a in listing.get("Metadata") or []
                        if a.get("ratingKey")]
    result.artists = len(artist_keys)

    seen = set()
    failing_in_a_row = 0
    try:
        for index, artist in enumerate(artist_keys):
            on_progress(index / len(artist_keys) * 100.0)
            extras = _container(_call("GET", url, token,
                                      "/library/metadata/%s/extras" % artist))
            for extra in extras.get("Metadata") or []:
                key = str(extra.get("ratingKey") or "")
                files = [f for f in _files(extra) if f.lower().endswith(VIDEO_EXTS)]
                if not key or not files or key in seen:
                    continue
                seen.add(key)
                result.videos += 1
                here = local_path(files[0], base, config.music_root)
                if here is None or not here.is_file():
                    result.not_here += 1
                    continue
                try:
                    art = embedded_art(here)
                except Exception:
                    art = None
                if art is None:
                    result.no_art += 1
                    continue
                digest = hashlib.sha1(art[0]).hexdigest()
                if sent_here.get(key) == digest:
                    result.unchanged += 1
                    continue
                try:
                    _call("POST", url, token, "/library/metadata/%s/thumb" % key,
                          data=art[0], content_type=art[1])
                except PlexError as exc:
                    result.failed += 1
                    result.reasons[str(exc)] += 1
                    failing_in_a_row += 1
                    if failing_in_a_row >= GIVE_UP_AFTER:
                        result.stopped = ("%d uploads in a row refused (%s) - this Plex "
                                          "may not take artwork for music videos"
                                          % (failing_in_a_row, exc))
                        return result
                    continue
                failing_in_a_row = 0
                sent_here[key] = digest
                result.sent += 1
                on_detail("sent artwork for %s" % (extra.get("title") or here.name))
                if result.sent % 25 == 0:
                    _save_state(state_path, state)
    finally:
        _save_state(state_path, state)
    on_progress(100.0)
    return result
