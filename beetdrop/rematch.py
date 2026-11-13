"""Correct a track that was matched to the wrong recording.

Matching is duration-first text similarity with no acoustic
fingerprinting, so a cover, a re-upload or a same-length different song
can get through - and anything it could not verify at all is filed under
_review/ with tags taken from YouTube. Both leave a file on disk whose
tags are wrong and which nothing else will ever revisit.

This lets a person say what the track actually is. It searches
MusicBrainz, describes the candidates so they can be told apart, and on
a choice re-tags the file and moves it to where those tags say it
belongs - out of _review/ and into the library proper, in the usual
case.

Nothing here guesses. Matching already had its turn; a person picking is
the whole point, so the candidates are shown as MusicBrainz returned
them rather than filtered by the checks that got it wrong.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .companion import carry_companion
from .config import Config
from .fulltags import FullTags, write_full_tags
from .library import (
    REVIEW_DIR,
    _flatten_media,
    _release_date,
    _tags_from_release_track,
    album_dir,
    place_file,
    track_filename,
)
from .matching import credit_ids, credit_name, select_release, select_release_for

AUDIO_EXTS = (".opus", ".ogg", ".mp3", ".m4a", ".flac")


def filing_root_for(config: Config, path: Path) -> Path:
    """Where a re-matched track is filed.

    With a folder set for new grabs, the music folder holds several
    libraries side by side ("explo", "Nick", "Both"), and a track stays in
    the one it is in: its first folder below the music folder. A track
    in _review, or not in such a folder, goes where new grabs go.
    Without one, the music folder is the library, as before.
    """
    if not config.grab_subfolder or not str(config.grab_subfolder).strip("/ "):
        return config.music_root
    filing = config.filing_root
    try:
        path.resolve().relative_to(filing.resolve())
        return filing
    except (ValueError, OSError):
        pass
    try:
        parts = path.resolve().relative_to(config.music_root.resolve()).parts
    except (ValueError, OSError):
        return filing
    if len(parts) >= 3 and parts[0] != REVIEW_DIR:
        return config.music_root / parts[0]
    return filing


def review_roots(config: Config) -> list:
    """The folders whose _review/ hold unverified grabs: where new grabs
    go, and the music folder itself, where they went before a folder for
    them was set."""
    roots = [config.filing_root]
    if config.filing_root != config.music_root:
        roots.append(config.music_root)
    return roots


def iter_unverified(root: Path):
    """Audio filed under _review/ - the grabs nothing could verify."""
    review = root / REVIEW_DIR
    if not review.is_dir():
        return
    for path in sorted(review.rglob("*")):
        if path.is_file() and path.suffix.lower() in AUDIO_EXTS:
            yield path


def search_library(root: Path, query: str, limit: int = 50) -> list:
    """Library files whose path contains this text.

    _review/ only holds what matching *knew* it could not verify. A match
    that was confidently wrong - a cover, a re-upload, the other band
    with a similar name - is filed into the library proper as verified,
    with wrong tags and a wrong folder, and nothing ever revisits it.
    Those are found by searching for what they are wrongly called, which
    is what a person sees in their player.

    The match is on the path rather than the tags because the path is
    built from the same wrong tags and costs nothing to read.
    """
    needles = [word for word in query.lower().split() if word]
    if not needles:
        return []
    found = []
    for path in sorted(root.rglob("*")):
        if not (path.is_file() and path.suffix.lower() in AUDIO_EXTS):
            continue
        try:
            haystack = str(path.relative_to(root)).lower()
        except ValueError:
            haystack = path.name.lower()
        if all(word in haystack for word in needles):
            found.append(path)
            if len(found) >= max(1, min(limit, 200)):
                break
    return found


def describe_recording(recording: dict, album: str = "") -> dict:
    """One candidate reduced to what a person needs to tell it apart.

    The release matters as much as the recording: two entries with the
    same title and artist are usually the studio cut and a live or
    compilation version, and only the release name says which.
    """
    releases = recording.get("releases") or []
    # The album asked for, when it is among them: that is the one it will
    # be filed under, so that is the one to show.
    chosen = select_release_for(releases, album) or (releases[0] if releases else {})
    millis = recording.get("length") or 0
    release_mbid = (chosen or {}).get("id") or ""
    return {
        "id": recording.get("id") or "",
        "title": recording.get("title") or "",
        "artist": credit_name(recording.get("artist-credit")) or "",
        "album": chosen.get("title", "") if chosen else "",
        "year": _release_date(chosen)[:4] if chosen else "",
        "duration": int(millis / 1000) if millis else 0,
        "release_count": len(releases),
        # Straight from the Cover Art Archive, loaded by the browser.
        # Plenty of releases have none, so the page hides what 404s
        # rather than this checking each one and costing a request.
        "artwork": ("https://coverartarchive.org/release/%s/front-250"
                    % release_mbid) if release_mbid else "",
    }


# Each search a step looser than the last, tried until one finds
# something: the exact phrases, with and without the album; then the
# words, ranked by MusicBrainz, with and without it. A title spelled a
# little differently there - punctuation, a "feat.", an edition - finds
# nothing by phrase, while Plex, matching against its own catalogue,
# found the song fine.
_NOTES = {
    ("exact", False): "Nothing on an album of that name - these are the song on any album.",
    ("words", True): "No exact match - these are the closest, ranked by MusicBrainz.",
    ("words", False): "No exact match, even without the album - these are the closest.",
}


def _steps(album: str) -> list:
    steps = [("exact", album)]
    if album:
        steps.append(("exact", ""))
    steps.append(("words", album))
    if album:
        steps.append(("words", ""))
    return steps


def _search(mb, how: str, title: str, artist: str, limit: int, album: str) -> list:
    if how == "exact":
        return mb.search_recordings(title, artist, limit=limit, album=album)
    loose = getattr(mb, "search_recordings_loose", None)
    return loose(title, artist, limit=limit, album=album) if loose else []


# A MusicBrainz page pasted in place of a title: the release (album) or
# recording a person already found there, so nothing need be searched.
_MB_LINK = re.compile(
    r"musicbrainz\.org/(release|recording)/"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)


def mb_link(text: str) -> Optional[tuple]:
    """("release" | "recording", its ID) for a MusicBrainz link, else None."""
    found = _MB_LINK.search(text or "")
    return (found.group(1).lower(), found.group(2).lower()) if found else None


def describe_release_track(release: dict, entry: dict) -> dict:
    """describe_recording for one track of a release, which picking files
    under exactly that release."""
    track = entry["track"]
    recording = track.get("recording") or {}
    credit = (track.get("artist-credit") or recording.get("artist-credit")
              or release.get("artist-credit"))
    millis = track.get("length") or recording.get("length") or 0
    release_mbid = release.get("id") or ""
    position = track.get("position") or track.get("number") or ""
    if entry.get("disc_total", 1) > 1 and position:
        position = "%s-%s" % (entry.get("disc_number", 1), position)
    return {
        "id": recording.get("id") or "",
        "title": track.get("title") or recording.get("title") or "",
        "artist": credit_name(credit) or "",
        "album": release.get("title") or "",
        "year": _release_date(release)[:4],
        "duration": int(millis / 1000) if millis else 0,
        "release_count": 1,
        "release_id": release_mbid,
        "position": str(position),
        "artwork": ("https://coverartarchive.org/release/%s/front-250"
                    % release_mbid) if release_mbid else "",
    }


def _from_link(mb, kind: str, mbid: str) -> tuple:
    if kind == "release":
        release = mb.get_release(mbid)
        rows = [describe_release_track(release, entry)
                for entry in _flatten_media(release)]
        rows = [row for row in rows if row["id"]]
        note = ("Every track on %s - pick this one." % (release.get("title") or "that release")
                if rows else "That release lists no tracks on MusicBrainz.")
        return rows, note
    recording = mb.get_recording(mbid)
    return ([describe_recording(recording)] if recording.get("id") else []), ""


def find_candidates(mb, title: str, artist: str, limit: int = 10,
                    album: str = "") -> tuple:
    """([what MusicBrainz has for this text, unfiltered], a note when the
    search had to be loosened to find it, else "").

    Deliberately not scored or rejected: the automatic checks are what
    produced the wrong answer, so applying them again would hide the
    right one.
    """
    title, artist, album = title.strip(), artist.strip(), album.strip()
    if not title:
        return [], ""
    link = mb_link(title)
    if link:
        return _from_link(mb, *link)
    for how, step_album in _steps(album):
        found = _search(mb, how, title, artist, limit, step_album)
        if found:
            note = "" if (how, step_album) == ("exact", album) else \
                _NOTES[(how, bool(step_album))]
            return [describe_recording(row, album) for row in found], note
    return [], ""


def search_candidates(mb, title: str, artist: str, limit: int = 10,
                      album: str = "") -> list:
    return find_candidates(mb, title, artist, limit=limit, album=album)[0]


def _tags_for(mb, recording: dict, fallback_title: str,
              fallback_artist: str, album: str = "") -> tuple:
    """(tags, release) for a recording a person chose - on the album they
    named, when the recording is on a release of that name."""
    chosen = select_release_for(recording.get("releases") or [], album)
    if chosen is None:
        return FullTags(
            title=recording.get("title") or fallback_title,
            artist=credit_name(recording.get("artist-credit")) or fallback_artist,
            album_artist=credit_name(recording.get("artist-credit")) or fallback_artist,
            album=recording.get("title") or fallback_title,
            recording_mbid=recording.get("id", ""),
            artist_mbids=credit_ids(recording.get("artist-credit")),
            unverified=False,
        ), None

    release = mb.get_release(chosen["id"])
    for entry in _flatten_media(release):
        if (entry["track"].get("recording") or {}).get("id") == recording.get("id"):
            return _tags_from_release_track(
                release, entry, fallback_title, fallback_artist), release
    # The recording is not on the release MusicBrainz just handed back -
    # its data drifts. Keep the identity and lose only the numbering.
    return FullTags(
        title=recording.get("title") or fallback_title,
        artist=credit_name(recording.get("artist-credit")) or fallback_artist,
        album_artist=credit_name(release.get("artist-credit")) or fallback_artist,
        album=release.get("title", ""),
        date=_release_date(release),
        recording_mbid=recording.get("id", ""),
        release_mbid=release.get("id", ""),
        release_group_mbid=(release.get("release-group") or {}).get("id", ""),
        artist_mbids=credit_ids(recording.get("artist-credit")),
        unverified=False,
    ), release


@dataclass
class Rematch:
    old_path: Path
    new_path: Path
    tags: FullTags
    moved_lyrics: bool = False
    moved_video: bool = False


def _current_title(path: Path) -> str:
    """The title the file is tagged with now, before it is re-tagged.

    A companion music video is named for this, so it is what finds the
    video to carry along.
    """
    try:
        import mutagen
        audio = mutagen.File(str(path), easy=True)
        return ((audio.get("title") or [""])[0] if audio else "") or ""
    except Exception:
        return ""


def _find_recording(mb, recording_id: str, title: str, artist: str,
                    album: str = "") -> Optional[dict]:
    """The chosen recording, from the same search that offered it.

    Re-running the search rather than looking the id up directly keeps
    this to endpoints the client already has, and the response is cached
    from moments ago when a person was reading it.
    """
    title, artist, album = title.strip(), artist.strip(), album.strip()
    if title and not mb_link(title):
        for how, step_album in _steps(album):
            for row in _search(mb, how, title, artist, 25, step_album):
                if row.get("id") == recording_id:
                    return row
    # Not offered by the search (a pasted link, or MusicBrainz ranking it
    # differently this time): look it up by its ID instead.
    lookup = getattr(mb, "get_recording", None)
    if lookup is None:
        return None
    try:
        row = lookup(recording_id)
    except Exception:
        return None
    return row if row.get("id") == recording_id else None


def _tags_on_release(mb, release_id: str, recording_id: str,
                     fallback_title: str, fallback_artist: str) -> FullTags:
    """Tags for a track of the release a person picked it from."""
    release = mb.get_release(release_id)
    for entry in _flatten_media(release):
        if (entry["track"].get("recording") or {}).get("id") == recording_id:
            return _tags_from_release_track(release, entry, fallback_title,
                                            fallback_artist)
    raise LookupError("that recording is not on that release")


def apply_choice(config: Config, mb, path: Path, recording_id: str,
                 title: str, artist: str, move: bool = True,
                 album: str = "", release_id: str = "") -> Rematch:
    """Re-tag this file as the chosen recording and re-file it.

    Moving is the default because a wrong match is not only wrong tags:
    the folder and filename were built from them too, so a track matched
    to the wrong song is sitting under the wrong artist and album. Fixing
    the tags and leaving it there would only half-fix it.

    move=False re-tags where it stands, for a library whose layout is not
    Beetdrop's - several collections under one mount, say, where this
    layout has no room for the first folder and moving would carry a
    track out of the collection it belongs to.

    The .lrc beside it comes along on a move. Lyrics are matched to the
    audio, not to the tags, so they are still right - and leaving them
    behind would strand them next to nothing. So does a companion music
    video, renamed for the new title, since Plex links it by title.
    """
    if not path.is_file():
        raise FileNotFoundError("that file is gone")
    if mb_link(title):
        title = ""      # a link, not a name to fall back on
    if release_id:
        # Picked from a release's track list: filed under that release.
        tags = _tags_on_release(mb, release_id, recording_id, title, artist)
    else:
        recording = _find_recording(mb, recording_id, title, artist, album)
        if recording is None:
            raise LookupError("MusicBrainz no longer offers that recording")
        tags, _release = _tags_for(mb, recording, title, artist, album.strip())
    old_title = _current_title(path)
    if not move:
        write_full_tags(path, tags)
        video = carry_companion(path, old_title, path)
        return Rematch(old_path=path, new_path=path, tags=tags,
                       moved_video=video is not None)

    destination = (album_dir(filing_root_for(config, path), tags)
                   / track_filename(tags, path.suffix.lstrip(".")))
    if destination.resolve() == path.resolve():
        # Already where it belongs; just correct the tags in place.
        write_full_tags(path, tags)
        video = carry_companion(path, old_title, path)
        return Rematch(old_path=path, new_path=path, tags=tags,
                       moved_video=video is not None)

    final = place_file(path, destination)
    try:
        write_full_tags(final, tags)
    except Exception:
        # The copy is the risky half and it succeeded; a tag write that
        # did not must not leave two files behind.
        final.unlink(missing_ok=True)
        raise

    sidecar = path.with_suffix(".lrc")
    moved = False
    if sidecar.is_file():
        try:
            shutil.copyfile(sidecar, final.with_suffix(".lrc"))
            sidecar.unlink()
            moved = True
        except OSError:
            moved = False
    # The music video was chosen to match the audio as downloaded, so it
    # is still the right video; it only needs the new folder and name.
    video = carry_companion(path, old_title, final)
    path.unlink(missing_ok=True)
    _prune_empty(path.parent, config.music_root)
    return Rematch(old_path=path, new_path=final, tags=tags, moved_lyrics=moved,
                   moved_video=video is not None)


def _prune_empty(directory: Path, root: Path) -> None:
    """Remove the folder a rematched track left behind, and its parents.

    A _review/ entry is one folder per track, so moving the file out
    leaves an empty one every time. Stops at the library root and at the
    first folder that still holds something.
    """
    try:
        root = root.resolve()
        current = directory.resolve()
    except OSError:
        return
    while current != root and root in current.parents:
        try:
            # A stray cover.jpg is not a reason to keep an empty folder.
            leftovers = [item for item in current.iterdir()
                         if item.name not in ("cover.jpg", "cover.png")]
            if leftovers:
                return
            for item in current.iterdir():
                item.unlink()
            current.rmdir()
        except OSError:
            return
        current = current.parent
