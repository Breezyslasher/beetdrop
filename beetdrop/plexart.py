"""Show music videos in Plex with the artwork embedded in them.

Plex does not read a music video's embedded cover art: it draws the
thumbnail of a music video extra from a frame. What it does take is an
image uploaded through its API - POST /library/metadata/{id}/posters
with the image as the body, as python-plexapi's uploadPoster does; only
the server's owner may. (POST .../thumb, the published API's spelling,
answered 404 on a real server.) python-plexapi offers that upload for
movies, artists and albums but not for clips, which is what extras are,
so a server may refuse it for music videos. Then Plex is asked instead
to refresh the item (PUT /library/metadata/{id}/refresh), which re-reads
the file - and with it, if this Plex reads it, the embedded art.

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
ARTIST = 8                 # Plex's metadata type numbers
TRACK = 10
STATE_FILE = "plex_artwork_sent.json"
GIVE_UP_AFTER = 5          # uploads failing in a row: Plex will not take them


@dataclass
class PlexArt:
    artists: int = 0
    videos: int = 0        # music video extras Plex listed
    sent: int = 0
    refreshed: int = 0     # upload refused; Plex asked to re-read the file instead
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


def by_tail(plex_file: str, music_root: Path) -> Optional[tuple]:
    """(the file here, the folder Plex sees as the music folder) found by
    the end of Plex's path alone: the longest tail of it that exists
    under the music folder here. A folder and a file name at least -
    a file name alone could be anyone's "Intro.mp4"."""
    parts = [p for p in plex_file.split("/") if p]
    for start in range(len(parts) - 1):
        tail = parts[start:]
        if len(tail) < 2:
            break
        here = music_root.joinpath(*tail)
        if here.is_file():
            return here, "/" + "/".join(parts[:start])
    return None


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
    chosen = plex.chosen_section(config, all_sections)
    if chosen:
        libraries = [chosen]
    elif base:
        libraries = [s for s in plex.sections_for(all_sections, base)
                     if s["type"] == "artist"]
    else:
        # Where Plex sees the music folder is not known; every music
        # library is walked and files are found by the ends of their
        # paths (by_tail), which also teaches the mapping.
        libraries = [s for s in all_sections if s["type"] == "artist"]
    if not libraries:
        raise PlexError("Plex has no music library%s" % (
            " holding %s" % base if base else ""))

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
    failing = [0]
    known = {"base": base}

    def find_here(plex_file: str) -> Optional[Path]:
        """The file here that Plex knows as `plex_file`; learns where Plex
        sees the music folder from the first one found by its tail."""
        here = (local_path(plex_file, known["base"], config.music_root)
                if known["base"] else None)
        if here is not None and here.is_file():
            return here
        found = by_tail(plex_file, config.music_root)
        if found is None:
            return None
        if not known["base"]:
            known["base"] = found[1]
            on_detail("Plex sees the music folder at %s" % found[1])
        return found[0]

    def give(key: str, title: str, here: Optional[Path]) -> bool:
        """Get this music video's embedded art into Plex. False when
        Plex has refused often enough in a row to stop."""
        if here is None or not here.is_file():
            result.not_here += 1
            return True
        try:
            art = embedded_art(here)
        except Exception:
            art = None
        if art is None:
            result.no_art += 1
            return True
        digest = hashlib.sha1(art[0]).hexdigest()
        if sent_here.get(key) == digest:
            result.unchanged += 1
            return True
        try:
            _call("POST", url, token, "/library/metadata/%s/posters" % key,
                  data=art[0], content_type=art[1])
        except PlexError as refused:
            # Not for extras, on this server. Have it re-read the file
            # instead; not remembered, since it is cheap and adds nothing
            # to Plex's store of images.
            result.reasons["upload refused: %s" % refused] += 1
            try:
                _call("PUT", url, token, "/library/metadata/%s/refresh" % key)
            except PlexError as exc:
                result.failed += 1
                failing[0] += 1
                if failing[0] >= GIVE_UP_AFTER:
                    result.stopped = ("%d in a row refused both an upload (%s) and a "
                                      "refresh (%s)" % (failing[0], refused, exc))
                    return False
                return True
            failing[0] = 0
            result.refreshed += 1
            on_detail("asked Plex to refresh %s" % (title or here.name))
            return True
        failing[0] = 0
        sent_here[key] = digest
        result.sent += 1
        on_detail("sent artwork for %s" % (title or here.name))
        if result.sent % 25 == 0:
            _save_state(state_path, state)
        return True

    try:
        # Music videos linked to a track - named like the track's file,
        # beside it - hang off the track as its primaryExtraKey. One
        # request a library lists every track with it.
        for library in libraries:
            listing = _container(_call("GET", url, token,
                                       "/library/sections/%s/all" % library["key"],
                                       params={"type": TRACK}))
            for track in listing.get("Metadata") or []:
                extra = str(track.get("primaryExtraKey") or "").rstrip("/")
                if not extra:
                    continue
                key = extra.rsplit("/", 1)[-1]
                audio = _files(track)
                if not key or key in seen or not audio:
                    continue
                seen.add(key)
                result.videos += 1
                song = find_here(audio[0])
                video = next((song.with_suffix(ext) for ext in VIDEO_EXTS
                              if song is not None and song.with_suffix(ext).is_file()),
                             None)
                if not give(key, track.get("title") or "", video):
                    return result

        # Music videos beside the artist rather than one track.
        for index, artist in enumerate(artist_keys):
            on_progress(index / max(1, len(artist_keys)) * 100.0)
            extras = _container(_call("GET", url, token,
                                      "/library/metadata/%s/extras" % artist))
            for extra in extras.get("Metadata") or []:
                key = str(extra.get("ratingKey") or "")
                files = [f for f in _files(extra) if f.lower().endswith(VIDEO_EXTS)]
                if not key or not files or key in seen:
                    continue
                seen.add(key)
                result.videos += 1
                if not give(key, extra.get("title") or "", find_here(files[0])):
                    return result
    finally:
        _save_state(state_path, state)
    on_progress(100.0)
    return result
