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


def embed_cover(video: Path, data: bytes, mime: str, mark: str = "") -> None:
    """Embed `data` as `video`'s cover art, via a copy that replaces it.
    `mark` records where artwork that was searched for came from."""
    from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

    temp = video.with_name(".%s.beetdrop-tmp" % video.name)
    try:
        shutil.copy2(video, temp)
        tagged = MP4(str(temp))
        if tagged.tags is None:
            tagged.add_tags()
        fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
        tagged.tags["covr"] = [MP4Cover(data, imageformat=fmt)]
        if mark:
            tagged.tags[SEARCH_MARK] = [MP4FreeForm(mark.encode("utf-8"))]
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


# "07 - ", "2-03 - ", "07. ", a disc and track "1-13 " - and "01 " with a
# leading zero, which no title starts with; "99 Luftballons" keeps its 99
# and "1-800-273-8255" its number.
_TRACK_NUMBER = re.compile(
    r"^\s*(?:(?:\d+-)?\d{1,3}\s*[-.]\s+|\d{1,2}-\d{1,3}\s+|0\d\s+)")
# Plex type suffixes and the like, however spelled ("-Video", "-Lyric"),
# hyphen against the name as Plex writes them - " - Live" with spaces is
# part of a title, and stays. Those that say what kind of video it is
# become words the matching understands, rather than vanishing.
_TYPE_TAIL = re.compile(
    r"(?<=\S)-(video|lyrics?|live|behindthescenes|concert|interview|"
    r"featurette|other)\s*$", re.IGNORECASE)
_TAIL_AS_WORDS = {"lyric": " (Lyrics)", "lyrics": " (Lyrics)", "live": " (Live)",
                  "concert": " (Live)"}
# A name cut short mid-bracket: "Galway Girl [Official A".
_OPEN_TAIL = re.compile(r"\s*[\[(][^\])]*$")
# A downloaded video is the same stream YouTube serves, so its length
# agrees to the second; this allows for rounding and container padding.
LENGTH_SLACK = 2.5
# With the title as good as identical, another upload of the same video
# - a few seconds of logo or silence either way - is still its picture.
NEAR_IDENTICAL = 0.97
LENGTH_SLACK_NEAR_IDENTICAL = 10.0
TITLE_AT_LEAST = 0.9
SEARCH_PAUSE = 1.0
GIVE_UP_AFTER = 5     # searches failing in a row: YouTube is refusing
# Written into a video whose artwork came from a search, so a later run
# can tell it from artwork a person or another tool put there.
SEARCH_MARK = "----:com.beetdrop:artwork"


def _close_bracket(match) -> str:
    text = match.group(0).rstrip()
    return text + ("]" if "[" in text else ")")


_NUMBER_ONLY = re.compile(r"^\s*\d{1,3}\s+(?=\S)")


def _numbered_folder(video: Path) -> bool:
    """Do the other files beside it start with a number and a space, as
    an album's tracks do ("10 FAITH", "11 A to Z")? Then a number in
    front of this one is a track number too, not part of its title -
    "99 Luftballons" alone in a folder keeps its 99."""
    try:
        others = [p.name for p in video.parent.iterdir() if p != video]
    except OSError:
        return False
    return sum(1 for name in others if _NUMBER_ONLY.match(name)) >= 2


def search_query(video: Path) -> str:
    """What to search YouTube for: the name, without the "07 - " a
    library adds, a type suffix like "-Video", or a bracket a length
    limit cut off part-way."""
    # A bracket a length limit cut off is closed, not dropped: what is in
    # it ("[Official Audio") still says what kind of upload this is, and
    # the title comparison sets bracketed words aside anyway.
    stem = _OPEN_TAIL.sub(_close_bracket, video.stem)
    stem = _TYPE_TAIL.sub(
        lambda m: _TAIL_AS_WORDS.get(m.group(1).lower(), ""), stem)
    stem = _OPEN_TAIL.sub(_close_bracket, stem)
    stem = _TRACK_NUMBER.sub("", stem).strip()
    if _numbered_folder(video):
        stem = _NUMBER_ONLY.sub("", stem).strip()
    return stem


def video_artist(video: Path, roots: Iterable[Path] = ()) -> str:
    """Who a video is by, when its name does not say: its own tags, the
    tags of the track named like it beside it, or the artist folder it
    is filed under (the first folder below the music folder)."""
    try:
        from mutagen.mp4 import MP4
        tags = MP4(str(video)).tags or {}
        for key in ("\xa9ART", "aART"):
            if tags.get(key) and str(tags[key][0]).strip():
                return str(tags[key][0]).strip()
    except Exception:
        pass
    try:
        from .backfill import AUDIO_EXTS, read_track_meta
        for ext in AUDIO_EXTS:
            song = video.with_suffix(ext)
            if song.is_file():
                meta = read_track_meta(song)
                if meta and meta[0]:
                    return meta[0]
    except Exception:
        pass
    for root in roots:
        try:
            relative = video.resolve().relative_to(root.resolve())
        except (ValueError, OSError):
            continue
        if len(relative.parts) >= 2:
            return relative.parts[0]
    return ""


def video_title(video: Path) -> str:
    """The song's title from tags, when there is one: the track named
    like the video beside it (a clean MusicBrainz title, as Beetdrop
    files it), else the video's own title tag. Cleaner than a file name
    a tool cut short or numbered - "1-13 From The D 2 The LBC"."""
    try:
        from .backfill import AUDIO_EXTS, read_track_meta
        for ext in AUDIO_EXTS:
            song = video.with_suffix(ext)
            if song.is_file():
                meta = read_track_meta(song)
                if meta and meta[1]:
                    return meta[1]
    except Exception:
        pass
    try:
        from mutagen.mp4 import MP4
        tags = MP4(str(video)).tags or {}
        if tags.get("\xa9nam") and str(tags["\xa9nam"][0]).strip():
            return str(tags["\xa9nam"][0]).strip()
    except Exception:
        pass
    return ""


def with_artist(query: str, artist: str) -> str:
    """The query with the artist in front, unless it already names them."""
    from .matching import normalize
    if not artist or normalize(artist) in normalize(query):
        return query
    return "%s - %s" % (artist, query)


# Words that label an upload rather than name the song; set aside when
# comparing titles, wherever they stand. What kind of upload it is gets
# checked on its own (_flavour), so nothing is lost by it.
_LABELS = re.compile(
    r"\b(?:official|music video|lyric video|video|audio|lyrics?|hd|hq|4k|"
    r"visuali[sz]er|mv)\b")


def _plain(title: str) -> str:
    from .matching import base_title
    words = _LABELS.sub(" ", base_title(title))
    return re.sub(r"\s+", " ", words).strip() or base_title(title)


def _flavour(title: str) -> tuple:
    """What kind of upload a title says it is: its live/remix/version
    qualifiers, and whether it is a lyric, audio or visualizer upload."""
    from .companion import _NOT_A_MUSIC_VIDEO
    from .matching import normalize, significant_qualifiers
    return (frozenset(significant_qualifiers(title)),
            bool(_NOT_A_MUSIC_VIDEO.search(normalize(title))))


# A file this close in length is that upload, to the second.
EXACT_LENGTH = 1.0


def _same_kind(want: tuple, got: tuple, gap: Optional[float]) -> bool:
    """Is the result the same kind of upload as the file?

    Lyric/audio uploads never pass for a video or the other way round -
    a lyric video runs exactly as long as the song. A live, remix, cover
    or numbered version only for a file whose name says the same. The one
    allowance: a bare "(Long Version)", "(Dirty Version)" on the result's
    side when its length is the file's to the second - that is the same
    upload, labelled. A version the file's own name asks for ("Version
    3") is still required.
    """
    if want[1] != got[1]:
        return False
    if want[0] == got[0]:
        return True
    extra, missing = got[0] - want[0], want[0] - got[0]
    return (not missing and extra <= {"version"}
            and gap is not None and gap <= EXACT_LENGTH)


def match_on_youtube(query: str, length: float, candidates: list,
                     bare: str = "", kind_from: str = "") -> tuple:
    """(the result to take, or None; a line on the closest one).

    All must hold, or nothing is taken:
    - the same kind of upload (_same_kind);
    - the title says the same thing once brackets, punctuation and
      labels are set aside - with or without an artist in front, whether
      the result's title carries it ("Elton John Crocodile Rock") or not;
    - the length is the file's own: to 2.5 seconds, or 10 when the title
      is as good as identical.
    `bare` is the title without an artist the file's name did not carry
    (one taken from its tags or folder): matching on that alone needs the
    length to the second, since without the artist a title is a guess.
    `kind_from` is the file's name, when the query's title came from
    tags: what kind of upload it is ("-Lyric", "[Official Audio]") is
    read from the name, which says it, not from a clean title that
    does not.
    Of several that pass, the closest in length.
    """
    from .companion import _ratio
    from .matching import normalize

    want = _plain(query)
    want_bare = _plain(bare) if bare else ""
    want_flavour = _flavour(kind_from or query)
    best, best_gap, closest, closest_score = None, None, "", None
    for candidate in candidates:
        raw = candidate.raw_title or candidate.title
        forms = set()
        for name in (raw, candidate.title):
            if not name:
                continue
            plain = _plain(name)
            forms.add(plain)
            for artist in candidate.artists[:2]:
                who = normalize(artist)
                if not who:
                    continue
                forms.add("%s %s" % (who, plain))
                if plain.startswith(who + " "):
                    forms.add(plain[len(who) + 1:])
        forms.discard("")
        title_score = max((_ratio(f, want) for f in forms), default=0.0)
        bare_score = max((_ratio(f, want_bare) for f in forms), default=0.0) \
            if want_bare else 0.0
        gap = (abs(candidate.duration_seconds - length)
               if candidate.duration_seconds and length else None)
        flavour = _flavour(raw)
        slack = (LENGTH_SLACK_NEAR_IDENTICAL if title_score >= NEAR_IDENTICAL
                 else LENGTH_SLACK)
        kind_ok = _same_kind(want_flavour, flavour, gap)
        if kind_ok and gap is not None and (
                (title_score >= TITLE_AT_LEAST and gap <= slack)
                or (bare_score >= NEAR_IDENTICAL and gap <= EXACT_LENGTH)):
            if best_gap is None or gap < best_gap:
                best, best_gap = candidate, gap
            continue
        shown = max(title_score, bare_score)
        score = shown - (min(gap, 60) / 60 if gap is not None else 1)
        if closest_score is None or score > closest_score:
            closest_score = score
            why = []
            if not kind_ok and flavour[0] != want_flavour[0]:
                why.append("a different version (%s)" % (
                    ", ".join(sorted(flavour[0] ^ want_flavour[0]))))
            if flavour[1] != want_flavour[1]:
                why.append("a lyric/audio upload" if flavour[1]
                           else "not a lyric/audio upload")
            closest = '"%s" - title %.0f%% alike, %s%s' % (
                raw, shown * 100,
                "length %+.0fs" % (candidate.duration_seconds - length)
                if gap is not None else "length unknown",
                ", " + ", ".join(why) if why else "")
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
    roots = list(roots)          # walked here and read again for artists
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
                named = search_query(video)
                bare = video_title(video) or named
                query = with_artist(bare, video_artist(video, roots))
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
                        found, closest = match_on_youtube(
                            query, length, candidates,
                            bare=bare if bare != query else "",
                            kind_from=named if bare != named else "")
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
                            embed_cover(video, *art, mark="youtube search: %s %s" % (
                                found.video_id, found.raw_title or found.title))
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
