"""Ask Plex to look at what was just filed.

Plex notices new files on its own schedule - a periodic scan, or a
filesystem watcher that often does not fire across a network share or a
container mount. So a grab can sit on disk for hours before it shows up.
After a job files something, Beetdrop asks for a partial scan of exactly
that folder: the same request Sonarr and Lidarr make, cheap enough to
send per grab, and it never rescans the whole library.

Two things make that harder than one URL:

- Which library? Plex scans by section, so the folder has to be matched
  to the section whose location contains it - the longest one, since
  libraries can nest.
- Whose path? Beetdrop and Plex usually see the same files at different
  places (/music here, /data/music there). The Plex path for the music
  library is a setting, and folders under the music root are mapped onto
  it. Anything outside the music root is passed through as-is.

Best effort throughout. A Plex that is down, asleep or unreachable is
reported on the job card and never fails a grab.

Requests follow Plex's published OpenAPI description (Plex Media Server
API 1.2.2): GET /library/sections/all to list libraries, and POST
/library/sections/{id}/refresh?path=... to scan one folder, with the
token in the X-Plex-Token header.
"""

from __future__ import annotations

import posixpath
import time
from pathlib import Path
from typing import Optional

import requests

TIMEOUT = 8
# The section list barely changes; asking for it on every grab would
# double the requests for nothing.
SECTIONS_TTL = 300
_cache: dict = {}


class PlexError(Exception):
    """Plex could not be asked, as distinct from saying no."""


def _call(method: str, url: str, token: str, path: str,
          params: Optional[dict] = None, allow_404: bool = False):
    """One request to the server, with the token in the header the API
    documents (X-Plex-Token) rather than the query string, where it would
    end up in Plex's own access logs."""
    try:
        response = requests.request(
            method, url.rstrip("/") + path, params=params, timeout=TIMEOUT,
            headers={"X-Plex-Token": token, "Accept": "application/json"})
    except requests.RequestException as exc:
        raise PlexError("could not reach Plex at %s (%s)"
                        % (url, exc.__class__.__name__)) from exc
    if response.status_code == 401:
        raise PlexError("Plex refused the token")
    if response.status_code == 404 and allow_404:
        return None
    if response.status_code == 503:
        # The API documents this for music refreshes specifically.
        raise PlexError("Plex cannot refresh a music library while the server "
                        "is signed out of plex.tv")
    if not response.ok:
        raise PlexError("Plex answered HTTP %d" % response.status_code)
    return response


def sections(url: str, token: str, fresh: bool = False) -> list:
    """Every library on the server: key, title, type and the folders it
    covers, as Plex sees them."""
    key = (url.rstrip("/"), token)
    cached = _cache.get(key)
    if cached and not fresh and time.monotonic() - cached[0] < SECTIONS_TTL:
        return cached[1]
    # /library/sections/all is the documented listing. Servers older than
    # the published API answer only the bare /library/sections, with the
    # same body - so a 404 there is a version, not a failure.
    response = (_call("GET", url, token, "/library/sections/all", allow_404=True)
                or _call("GET", url, token, "/library/sections"))
    try:
        body = response.json()
    except ValueError as exc:
        raise PlexError("Plex sent something that is not a library list") from exc
    found = []
    for directory in (body.get("MediaContainer") or {}).get("Directory") or []:
        found.append({
            "key": str(directory.get("key", "")),
            "title": directory.get("title", ""),
            "type": directory.get("type", ""),
            "locations": [loc.get("path", "") for loc in directory.get("Location") or []
                          if loc.get("path")],
        })
    _cache[key] = (time.monotonic(), found)
    return found


def plex_path(local: Path, music_root: Path, plex_music_path: str) -> str:
    """Where Plex sees `local`.

    Folders under the music root are re-rooted onto the path Plex knows
    the music library by. With no mapping set, both see the same paths.
    """
    if not plex_music_path:
        return local.as_posix()
    try:
        relative = local.resolve().relative_to(music_root.resolve())
    except (ValueError, OSError):
        return local.as_posix()
    base = plex_music_path.rstrip("/") or "/"
    parts = relative.as_posix()
    return base if parts in ("", ".") else posixpath.join(base, parts)


def _contains(location: str, path: str) -> bool:
    location = location.rstrip("/") or "/"
    return path == location or path.startswith(location.rstrip("/") + "/")


def section_for(all_sections: list, path: str) -> Optional[dict]:
    """The library that holds `path`: the deepest location containing it,
    so a library inside another is chosen over the one around it."""
    best, depth = None, -1
    for section in all_sections:
        for location in section["locations"]:
            if _contains(location, path) and len(location) > depth:
                best, depth = section, len(location)
    return best


def scan_folder(config, folder: Path) -> str:
    """Ask Plex to scan `folder`, and say what happened in one line.

    Returns "" when Plex is not configured, so a job card for someone who
    does not use Plex says nothing about it.
    """
    if not (config.plex_url and config.plex_token):
        return ""
    target = plex_path(folder, config.music_root, config.plex_music_path)
    try:
        section = section_for(sections(config.plex_url, config.plex_token), target)
        if section is None:
            # Perhaps a library was added since the list was cached.
            section = section_for(
                sections(config.plex_url, config.plex_token, fresh=True), target)
        if section is None:
            return ("Plex has no library containing %s - check where Plex sees "
                    "the music folder in Settings" % target)
        # POST, as the API documents it: a refresh changes server state.
        # `path` restricts it to that folder, so the library is never
        # rescanned whole for one grab.
        _call("POST", config.plex_url, config.plex_token,
              "/library/sections/%s/refresh" % section["key"],
              params={"path": target})
        return "Plex is scanning it into %s" % section["title"]
    except PlexError as exc:
        return "Plex was not told: %s" % exc


def check(config) -> dict:
    """For the Settings page: can Plex be reached, and which library will
    the music folder be scanned into."""
    if not (config.plex_url and config.plex_token):
        return {"ok": False, "detail": "Set the Plex address and token first."}
    try:
        all_sections = sections(config.plex_url, config.plex_token, fresh=True)
    except PlexError as exc:
        return {"ok": False, "detail": str(exc)}
    target = plex_path(config.music_root, config.music_root, config.plex_music_path)
    section = section_for(all_sections, target)
    libraries = [{"title": s["title"], "type": s["type"], "locations": s["locations"]}
                 for s in all_sections]
    if section is None:
        return {"ok": False, "libraries": libraries,
                "detail": ("Plex answered, but none of its libraries contains %s. "
                           "Set where Plex sees your music folder - one of the "
                           "locations listed." % target)}
    return {"ok": True, "libraries": libraries, "section": section["title"],
            "detail": "Connected. Grabs will be scanned into \"%s\"." % section["title"]}
