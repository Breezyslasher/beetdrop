"""Full tag writing for library mode.

Picard-compatible names so Plex, Navidrome, Picard, and beets all agree:
the recording MBID goes to MUSICBRAINZ_TRACKID / UFID:http://musicbrainz.org /
the iTunes freeform "MusicBrainz Track Id", following Picard rather than
inventing a scheme.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from mutagen.flac import Picture
from mutagen.id3 import APIC, COMM, ID3, TALB, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, TXXX, UFID, USLT
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from mutagen.oggopus import OggOpus


@dataclass
class FullTags:
    title: str = ""
    artist: str = ""
    album_artist: str = ""
    album: str = ""
    date: str = ""
    track_number: int = 0
    track_total: int = 0
    disc_number: int = 0
    disc_total: int = 0
    recording_mbid: str = ""
    release_mbid: str = ""
    release_group_mbid: str = ""
    artist_mbids: list = field(default_factory=list)
    lyrics: str = ""
    unverified: bool = False

    @property
    def year(self) -> str:
        return self.date[:4] if len(self.date) >= 4 else ""


_UNVERIFIED_NOTE = "beetdrop: unverified metadata derived from YouTube only"


def write_full_tags(path: Path, tags: FullTags,
                    cover: Optional[bytes] = None,
                    cover_mime: str = "image/jpeg") -> None:
    suffix = path.suffix.lstrip(".").lower()
    if suffix in ("opus", "ogg"):
        _write_vorbis(path, tags, cover, cover_mime)
    elif suffix == "mp3":
        _write_id3(path, tags, cover, cover_mime)
    elif suffix in ("m4a", "mp4"):
        _write_mp4(path, tags, cover, cover_mime)
    else:
        raise ValueError("no tag writer for .%s" % suffix)


def _write_vorbis(path, tags, cover, cover_mime):
    audio = OggOpus(str(path))
    audio["TITLE"] = [tags.title]
    audio["ARTIST"] = [tags.artist]
    if tags.album_artist:
        audio["ALBUMARTIST"] = [tags.album_artist]
    if tags.album:
        audio["ALBUM"] = [tags.album]
    if tags.date:
        audio["DATE"] = [tags.date]
    if tags.track_number:
        audio["TRACKNUMBER"] = [str(tags.track_number)]
    if tags.track_total:
        audio["TRACKTOTAL"] = [str(tags.track_total)]
    if tags.disc_number:
        audio["DISCNUMBER"] = [str(tags.disc_number)]
    if tags.disc_total:
        audio["DISCTOTAL"] = [str(tags.disc_total)]
    if tags.recording_mbid:
        audio["MUSICBRAINZ_TRACKID"] = [tags.recording_mbid]
    if tags.release_mbid:
        audio["MUSICBRAINZ_ALBUMID"] = [tags.release_mbid]
    if tags.release_group_mbid:
        audio["MUSICBRAINZ_RELEASEGROUPID"] = [tags.release_group_mbid]
    if tags.artist_mbids:
        audio["MUSICBRAINZ_ARTISTID"] = list(tags.artist_mbids)
    if tags.lyrics:
        audio["LYRICS"] = [tags.lyrics]
    if tags.unverified:
        audio["COMMENT"] = [_UNVERIFIED_NOTE]
    if cover:
        picture = Picture()
        picture.type = 3
        picture.mime = cover_mime
        picture.data = cover
        audio["METADATA_BLOCK_PICTURE"] = [
            base64.b64encode(picture.write()).decode("ascii")]
    audio.save()


def _write_id3(path, tags, cover, cover_mime):
    audio = MP3(str(path))
    if audio.tags is None:
        audio.add_tags()
    id3: ID3 = audio.tags
    id3.add(TIT2(encoding=3, text=[tags.title]))
    id3.add(TPE1(encoding=3, text=[tags.artist]))
    if tags.album_artist:
        id3.add(TPE2(encoding=3, text=[tags.album_artist]))
    if tags.album:
        id3.add(TALB(encoding=3, text=[tags.album]))
    if tags.date:
        id3.add(TDRC(encoding=3, text=[tags.date]))
    if tags.track_number:
        text = ("%d/%d" % (tags.track_number, tags.track_total)
                if tags.track_total else str(tags.track_number))
        id3.add(TRCK(encoding=3, text=[text]))
    if tags.disc_number:
        text = ("%d/%d" % (tags.disc_number, tags.disc_total)
                if tags.disc_total else str(tags.disc_number))
        id3.add(TPOS(encoding=3, text=[text]))
    if tags.recording_mbid:
        id3.add(UFID(owner="http://musicbrainz.org",
                     data=tags.recording_mbid.encode("ascii")))
    if tags.release_mbid:
        id3.add(TXXX(encoding=3, desc="MusicBrainz Album Id", text=[tags.release_mbid]))
    if tags.release_group_mbid:
        id3.add(TXXX(encoding=3, desc="MusicBrainz Release Group Id",
                     text=[tags.release_group_mbid]))
    if tags.artist_mbids:
        id3.add(TXXX(encoding=3, desc="MusicBrainz Artist Id", text=list(tags.artist_mbids)))
    if tags.lyrics:
        id3.add(USLT(encoding=3, lang="eng", desc="", text=tags.lyrics))
    if tags.unverified:
        id3.add(COMM(encoding=3, lang="eng", desc="", text=[_UNVERIFIED_NOTE]))
    if cover:
        id3.add(APIC(encoding=3, mime=cover_mime, type=3, desc="", data=cover))
    audio.save(v2_version=4)


def _write_mp4(path, tags, cover, cover_mime):
    audio = MP4(str(path))

    def freeform(name, values):
        audio["----:com.apple.iTunes:" + name] = [
            MP4FreeForm(v.encode("utf-8")) for v in values]

    audio["\xa9nam"] = [tags.title]
    audio["\xa9ART"] = [tags.artist]
    if tags.album_artist:
        audio["aART"] = [tags.album_artist]
    if tags.album:
        audio["\xa9alb"] = [tags.album]
    if tags.date:
        audio["\xa9day"] = [tags.date]
    if tags.track_number:
        audio["trkn"] = [(tags.track_number, tags.track_total)]
    if tags.disc_number:
        audio["disk"] = [(tags.disc_number, tags.disc_total)]
    if tags.recording_mbid:
        freeform("MusicBrainz Track Id", [tags.recording_mbid])
    if tags.release_mbid:
        freeform("MusicBrainz Album Id", [tags.release_mbid])
    if tags.release_group_mbid:
        freeform("MusicBrainz Release Group Id", [tags.release_group_mbid])
    if tags.artist_mbids:
        freeform("MusicBrainz Artist Id", list(tags.artist_mbids))
    if tags.lyrics:
        audio["\xa9lyr"] = [tags.lyrics]
    if tags.unverified:
        audio["\xa9cmt"] = [_UNVERIFIED_NOTE]
    if cover:
        image_format = (MP4Cover.FORMAT_PNG if cover_mime == "image/png"
                        else MP4Cover.FORMAT_JPEG)
        audio["covr"] = [MP4Cover(cover, imageformat=image_format)]
    audio.save()
