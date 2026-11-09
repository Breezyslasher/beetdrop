"""Ask Plex to look at what was just filed.

Plex notices new files on its own schedule - a periodic scan, or a
filesystem watcher that often does not fire across a network share or a
container mount. So a grab can sit on disk for hours before it shows up.
After a job files something, Beetdrop asks for a partial scan of exactly
that folder: the same request Sonarr and Lidarr make, cheap enough to
send per grab, and it never rescans the whole library.

Two things make that harder than one URL:

- Which library? Plex scans by section, so the folder has to be matched
  to the sections whose locations contain it - every one of them, since
  a folder can be in several libraries and Plex indexes it in each.
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
import re
import time
from pathlib import Path, PurePosixPath
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
          params: Optional[dict] = None, allow_404: bool = False,
          timeout: float = TIMEOUT):
    """One request to the server, with the token in the header the API
    documents (X-Plex-Token) rather than the query string, where it would
    end up in Plex's own access logs."""
    try:
        response = requests.request(
            method, url.rstrip("/") + path, params=params, timeout=timeout,
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


def sections(url: str, token: str, fresh: bool = False,
             timeout: float = TIMEOUT) -> list:
    """Every library on the server: key, title, type and the folders it
    covers, as Plex sees them."""
    key = (url.rstrip("/"), token)
    cached = _cache.get(key)
    if cached and not fresh and time.monotonic() - cached[0] < SECTIONS_TTL:
        return cached[1]
    # /library/sections/all is the documented listing. Servers older than
    # the published API answer only the bare /library/sections, with the
    # same body - so a 404 there is a version, not a failure.
    response = (_call("GET", url, token, "/library/sections/all",
                      allow_404=True, timeout=timeout)
                or _call("GET", url, token, "/library/sections", timeout=timeout))
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


def sections_for(all_sections: list, path: str) -> list:
    """Every library that holds `path`.

    One folder can belong to several libraries - the same "Both" folder
    in two people's music libraries, or a library inside another - and
    Plex indexes it in each, so each has to be asked to scan it.
    """
    return [section for section in all_sections
            if any(_contains(location, path) for location in section["locations"])]


def _titles(found: list) -> str:
    return " and ".join('"%s"' % s["title"] for s in found)


def mapping(config, all_sections: list) -> tuple:
    """Where Plex sees the music folder, and how that was found.

    "set" - typed in Settings. "same" - Plex sees it at the same path.
    "mount" - worked out from which folder of the host's disk is mounted
    as the music folder (see suggest_mapping). ("", "") when none of
    those can tell, which still leaves a chosen library scannable whole.
    """
    if config.plex_music_path:
        return config.plex_music_path.rstrip("/") or "/", "set"
    same = config.music_root.as_posix()
    if sections_for(all_sections, same):
        return same, "same"
    guess = suggest_mapping(all_sections, source_parts(config.music_root))
    return (guess, "mount") if guess else ("", "")


def chosen_section(config, all_sections: list) -> Optional[dict]:
    key = getattr(config, "plex_section", "")
    return next((s for s in all_sections if s["key"] == key), None) if key else None


def plan(config, all_sections: list, folder: Path) -> tuple:
    """(libraries to scan, the Plex path to restrict the scan to or "").

    With a library chosen in Settings, that library alone: just the
    folder when Beetdrop knows where Plex sees it inside that library,
    the whole library when it does not - Plex skips files it already
    has, so that is slower, never wrong. Left on automatic, every library
    holding the folder.
    """
    base, _how = mapping(config, all_sections)
    target = plex_path(folder, config.music_root, base)
    chosen = chosen_section(config, all_sections)
    if chosen is not None:
        inside = any(_contains(loc, target) for loc in chosen["locations"])
        return [chosen], target if inside else ""
    return sections_for(all_sections, target), target


def scan_folder(config, folder: Path) -> str:
    """Ask Plex to scan `folder`, and say what happened in one line.

    Returns "" when Plex is not configured, so a job card for someone who
    does not use Plex says nothing about it.
    """
    if not (config.plex_url and config.plex_token):
        return ""
    try:
        found, target = plan(config, sections(config.plex_url, config.plex_token), folder)
        if not found:
            # Perhaps a library was added since the list was cached.
            found, target = plan(
                config, sections(config.plex_url, config.plex_token, fresh=True), folder)
        if not found:
            if getattr(config, "plex_section", ""):
                return ("Plex no longer has the library chosen in Settings - "
                        "pick it again")
            return ("Plex has no library containing %s - pick the library to "
                    "scan in Settings" % (target or folder))
        # POST, as the API documents it: a refresh changes server state.
        # `path` restricts it to that folder, so the library is not
        # rescanned whole for one grab when the folder is known.
        for section in found:
            _call("POST", config.plex_url, config.plex_token,
                  "/library/sections/%s/refresh" % section["key"],
                  params={"path": target} if target else None)
        if not target:
            return "Plex is rescanning all of %s" % _titles(found)
        return "Plex is scanning it into %s" % _titles(found)
    except PlexError as exc:
        return "Plex was not told: %s" % exc


MOUNTINFO = Path("/proc/self/mountinfo")


def _unescape(field: str) -> str:
    # mountinfo writes space, tab, newline and backslash as octal escapes.
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


def source_parts(music_root: Path, mountinfo: Optional[str] = None) -> list:
    """The music folder's path on the filesystem it was mounted from.

    In a container, /music is a bind mount, and the kernel records which
    folder of the host's disk it came from (mountinfo's "root" field):
    /Nas-files/Media/Music/Nick, say, for a disk the host mounts at
    /srv/dev-disk-by-uuid-.... That is the tail of the path Plex knows
    the same folder by, which is enough to pick it out of Plex's list.
    Empty when it cannot be told - no /proc, or not a bind mount.
    """
    if mountinfo is None:
        try:
            mountinfo = MOUNTINFO.read_text()
        except OSError:
            return []
    root = PurePosixPath(music_root.as_posix())
    best, best_point = None, None
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        source, point = PurePosixPath(_unescape(fields[3])), PurePosixPath(_unescape(fields[4]))
        if (root == point or point in root.parents) and (
                best_point is None or len(point.parts) > len(best_point.parts)):
            best, best_point = source, point
    if best is None or best == PurePosixPath("/"):
        return []
    return [p for p in (best / root.relative_to(best_point)).parts if p != "/"]


def suggest_mapping(all_sections: list, tail: list) -> str:
    """The Plex folder that is most likely Beetdrop's music folder.

    Matches the end of a Plex library location against the music folder's
    path on its own disk (see source_parts), so
    /srv/dev-disk-by-uuid-1FF6/Nas-files/Media/Music/Nick is found for a
    folder mounted from /Nas-files/Media/Music/Nick. Two matching folder
    names at least, so a lone "music" decides nothing. Music libraries
    win a tie.
    """
    best, best_score = "", None
    for section in all_sections:
        for location in section["locations"]:
            parts = [p for p in PurePosixPath(location).parts if p != "/"]
            for end in range(len(tail), 0, -1):
                k = 0
                while (k < min(end, len(parts))
                       and tail[end - 1 - k] == parts[len(parts) - 1 - k]):
                    k += 1
                if k < 2:
                    continue
                score = (k, section["type"] == "artist", end)
                if best_score is None or score > best_score:
                    rest = tail[end:]
                    best = posixpath.join(location.rstrip("/"), *rest) if rest else \
                        location.rstrip("/")
                    best_score = score
    return best


def check(config) -> dict:
    """For the Settings page: can Plex be reached, which libraries it
    has (to pick from), and where grabs will be scanned."""
    if not (config.plex_url and config.plex_token):
        return {"ok": False, "detail": "Set the Plex address and token first."}
    try:
        all_sections = sections(config.plex_url, config.plex_token, fresh=True)
    except PlexError as exc:
        return {"ok": False, "detail": str(exc)}
    base, how = mapping(config, all_sections)
    found, target = plan(config, all_sections, config.music_root)
    libraries = [{"key": s["key"], "title": s["title"], "type": s["type"],
                  "locations": s["locations"]} for s in all_sections]
    result = {"libraries": libraries, "mapped": base, "mapped_how": how,
              "chosen": getattr(config, "plex_section", "")}
    if not found:
        if result["chosen"]:
            # Deleted, or a different server since it was picked.
            return dict(result, ok=False, detail=(
                "Plex answered, but the library chosen in Settings is gone. "
                "Pick the library to scan."))
        return dict(result, ok=False, detail=(
            "Plex answered, but none of its libraries contains %s. Pick the "
            "library to scan." % (target or config.music_root)))
    result.update(ok=True, section=found[0]["title"],
                  sections=[s["title"] for s in found])
    if not target:
        result["detail"] = (
            "Connected. Each grab rescans all of %s, since Beetdrop cannot tell "
            "which of its folders is %s. To scan only the new folder, set where "
            "Plex sees the music folder." % (_titles(found), config.music_root))
    elif how == "mount":
        result["detail"] = (
            "Connected. Grabs will be scanned into %s, at %s - the folder "
            "mounted here as %s." % (_titles(found), target, config.music_root))
    else:
        result["detail"] = "Connected. Grabs will be scanned into %s." % _titles(found)
    return result
