"""One grab, end to end: download, seed-tag, verify, atomic inbox handoff.

The success outcome is "handed off to the inbox", not "imported" —
whether and how the file imports is beets' decision, and Beetdrop does
not know.

Stage and progress callbacks exist so the web layer's job queue can
mirror the pipeline; the CLI passes simple printers.
"""

from __future__ import annotations

import os
import random
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import threading
from dataclasses import replace

from .config import Config
from .download import DownloadError, download_audio
from .fulltags import FullTags, write_full_tags
from .handoff import deliver, same_filesystem
from .library import (
    album_dir,
    place_file,
    resolve_album,
    resolve_track,
    track_filename,
    write_cover_file,
)
from .mb import MusicBrainzClient
from .paths import grab_folder_name, sanitize_segment
from .search import AlbumLookup, Result, lookup_album, lookup_video
from .seedtags import SeedTags, seed_from_result, verify_audio, write_seed_tags

# Stage labels, in pipeline order. "queued", "done" and "failed" are owned
# by the job layer. "matching" only occurs in library mode.
STAGES = ("searching", "downloading", "extracting", "matching", "tagging", "moving")


@dataclass
class GrabOutcome:
    inbox_path: Path
    result: Result
    verified: bool = True  # library mode: False means filed under _review


def _noop(*args) -> None:
    pass


# One MusicBrainz client per cache file, shared across worker threads so
# the 1-request-per-second limit holds process-wide.
_mb_clients: dict = {}
_mb_lock = threading.Lock()


def get_mb_client(config: Config) -> MusicBrainzClient:
    cache_path = config.config_dir / "mb_cache.sqlite3"
    with _mb_lock:
        client = _mb_clients.get(str(cache_path))
        if client is None:
            client = MusicBrainzClient(cache_path)
            _mb_clients[str(cache_path)] = client
        return client


def _finish_into_library(config: Config, audio_path: Path, tags: FullTags,
                         cover) -> Path:
    """Tag fully and file one track into the music library (or _review)."""
    cover_bytes, cover_mime = (cover[0], cover[1]) if cover else (None, "image/jpeg")
    write_full_tags(audio_path, tags, cover_bytes, cover_mime)
    directory = album_dir(config.music_root, tags)
    final = place_file(audio_path, directory / track_filename(
        tags, audio_path.suffix.lstrip(".")))
    if cover_bytes and not tags.unverified:
        write_cover_file(directory, cover_bytes, cover_mime)
    return final


def run_grab(
    video_id: str,
    config: Config,
    fmt: str = "",
    bitrate: str = "",
    on_stage: Callable[[str], None] = _noop,
    on_progress: Callable[[float], None] = _noop,
    on_resolved: Callable[[Result], None] = _noop,
    logger=None,
) -> GrabOutcome:
    fmt = fmt or config.output_format
    bitrate = bitrate or config.bitrate

    job_id = uuid.uuid4().hex[:12]
    scratch = config.scratch_root / job_id
    config.scratch_root.mkdir(parents=True, exist_ok=True)
    cross_fs = not same_filesystem(config.scratch_root, config.inbox)

    on_stage("searching")
    result = lookup_video(video_id)
    on_resolved(result)

    def progress_hook(data: dict) -> None:
        if data.get("status") != "downloading":
            return
        total = data.get("total_bytes") or data.get("total_bytes_estimate")
        downloaded = data.get("downloaded_bytes")
        if total and downloaded is not None:
            on_progress(min(100.0, downloaded / total * 100.0))

    def postprocessor_hook(data: dict) -> None:
        if data.get("status") == "started":
            on_stage("extracting")

    try:
        on_stage("downloading")
        audio_path = download_audio(
            video_id, scratch, fmt=fmt, bitrate=bitrate,
            cookies_file=config.cookies_file,
            progress_hook=progress_hook,
            postprocessor_hook=postprocessor_hook,
            logger=logger,
        )

        verify_audio(audio_path)

        if config.mode == "library":
            on_stage("matching")
            mb = get_mb_client(config)
            resolution = resolve_track(
                mb, result.title, result.primary_artist, result.duration_seconds)
            tags = resolution.tags
            cover = mb.fetch_cover(
                release_mbid=tags.release_mbid,
                release_group_mbid=tags.release_group_mbid,
                thumbnail_url=result.thumbnail_url)
            on_stage("tagging")
            on_stage("moving")
            final = _finish_into_library(config, audio_path, tags, cover)
            return GrabOutcome(inbox_path=final, result=result,
                               verified=resolution.matched)

        on_stage("tagging")
        seed = seed_from_result(result)
        write_seed_tags(audio_path, seed)

        on_stage("moving")
        folder_name = grab_folder_name(seed.albumartist, seed.title)
        # Stage the final one-grab-one-folder unit inside scratch, then
        # hand the whole directory off in a single rename.
        staged = scratch / "staged" / folder_name
        staged.mkdir(parents=True)
        final_audio = staged / ("%s.%s" % (folder_name, audio_path.suffix.lstrip(".")))
        audio_path.rename(final_audio)
        destination = deliver(staged, config.inbox, folder_name, cross_fs)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    return GrabOutcome(inbox_path=destination, result=result)


@dataclass
class AlbumGrabOutcome:
    inbox_path: Path
    album_title: str
    album_artist: str
    delivered: int
    # {"n": track_number, "title", "reason"} per track that could not be
    # grabbed - structured so a later retry can target exactly these.
    failed: list[dict]
    verified: bool = True  # library mode: False means filed under _review


def failures_text(failed: list[dict]) -> str:
    return "; ".join("%s: %s" % (f.get("title", "?"), f.get("reason", "?")) for f in failed)


def run_album_grab(
    browse_id: str,
    config: Config,
    fmt: str = "",
    bitrate: str = "",
    on_stage: Callable[[str], None] = _noop,
    on_progress: Callable[[float], None] = _noop,
    on_resolved: Callable[[str, str], None] = _noop,  # (album title, album artist)
    on_detail: Callable[[str], None] = _noop,
    logger=None,
    only_tracks: Optional[set] = None,
    patch_into: Optional[Path] = None,
) -> AlbumGrabOutcome:
    """Grab a whole album: every playable track downloaded, seed-tagged
    with its track number, and delivered as ONE album folder in a single
    atomic rename - a complete album folder is beets' best import unit.

    Individual track failures do not abort the album; they are collected
    and reported. Only an album with zero successful tracks fails.

    Retry mode: only_tracks limits the run to those track numbers, and
    patch_into slots the recovered files into an existing inbox folder
    (file-by-file, temp-then-replace, never overwriting) when it still
    exists - so a transient failure gets patched into the album before
    beets imports it. In this mode zero recoveries is a result, not an
    error: the album itself was already delivered.
    """
    fmt = fmt or config.output_format
    bitrate = bitrate or config.bitrate

    job_id = uuid.uuid4().hex[:12]
    scratch = config.scratch_root / job_id
    config.scratch_root.mkdir(parents=True, exist_ok=True)
    cross_fs = not same_filesystem(config.scratch_root, config.inbox)

    on_stage("searching")
    lookup: AlbumLookup = lookup_album(browse_id)
    album_artist = lookup.album.artist_display or "Unknown Artist"
    on_resolved(lookup.album.title, album_artist)

    tracks = lookup.tracks
    unavailable = lookup.unavailable
    if only_tracks is not None:
        tracks = [t for t in tracks if t.track_number in only_tracks]
        unavailable = [u for u in unavailable if u.get("n") in only_tracks]
    if not tracks and only_tracks is None:
        raise DownloadError("album has no playable tracks")

    folder_name = grab_folder_name(album_artist, lookup.album.title)
    failed = [
        {"n": u.get("n"), "title": u.get("title", "?"),
         "reason": "not available on YouTube Music"}
        for u in unavailable
    ]
    total = len(tracks)
    delivered = 0

    library = config.mode == "library"
    resolution = None
    cover = None
    album_level = None
    if library:
        on_stage("matching")
        mb = get_mb_client(config)
        try:
            resolution = resolve_album(mb, lookup.album.title, album_artist,
                                       lookup.tracks)
        except Exception:
            resolution = None  # MB down: file unverified rather than fail
        if resolution is not None:
            release = resolution.release
            album_level = FullTags(
                artist=album_artist,
                title=lookup.album.title,
                album_artist=next(iter(resolution.track_tags.values())).album_artist
                if resolution.track_tags else album_artist,
                album=release.get("title", lookup.album.title),
                date=release.get("date", "") or lookup.album.year,
                unverified=False,
            )
            cover = mb.fetch_cover(
                release_mbid=release.get("id", ""),
                release_group_mbid=(release.get("release-group") or {}).get("id", ""),
                thumbnail_url=lookup.album.thumbnail_url)
        else:
            album_level = FullTags(
                artist=album_artist, title=lookup.album.title,
                album_artist=album_artist, album=lookup.album.title,
                date=lookup.album.year, unverified=True)
            cover = mb.fetch_cover(thumbnail_url=lookup.album.thumbnail_url)
        library_dir = album_dir(config.music_root, album_level)

    try:
        staged = scratch / "staged" / folder_name
        staged.mkdir(parents=True)
        on_stage("downloading")
        delay_low, delay_high = config.track_delay_range()
        for index, track in enumerate(tracks):
            if index > 0 and delay_high > 0:
                # Randomized spacing between tracks: back-to-back
                # downloads look bot-like to YouTube's throttling.
                time.sleep(random.uniform(delay_low, delay_high))
            on_detail("track %d/%d: %s" % (index + 1, total, track.title))

            def progress_hook(data: dict, _base=index) -> None:
                if data.get("status") != "downloading":
                    return
                total_bytes = data.get("total_bytes") or data.get("total_bytes_estimate")
                downloaded = data.get("downloaded_bytes")
                if total_bytes and downloaded is not None:
                    fraction = min(1.0, downloaded / total_bytes)
                    on_progress((_base + fraction) / total * 100.0)

            try:
                track_dir = scratch / ("t%02d" % index)
                audio_path = download_audio(
                    track.video_id, track_dir, fmt=fmt, bitrate=bitrate,
                    cookies_file=config.cookies_file, progress_hook=progress_hook,
                    logger=logger,
                )
                verify_audio(audio_path)
                if library:
                    matched_tags = (resolution.track_tags.get(track.track_number)
                                    if resolution else None)
                    if matched_tags is not None:
                        tags = replace(matched_tags)
                    else:
                        tags = FullTags(
                            title=track.title,
                            artist=track.artist_display or album_artist,
                            album_artist=album_level.album_artist,
                            album=album_level.album,
                            date=album_level.date,
                            track_number=track.track_number or (index + 1),
                            track_total=lookup.album.track_count or 0,
                            unverified=album_level.unverified,
                        )
                    cover_bytes, cover_mime = ((cover[0], cover[1]) if cover
                                               else (None, "image/jpeg"))
                    write_full_tags(audio_path, tags, cover_bytes, cover_mime)
                    place_file(
                        audio_path,
                        library_dir / track_filename(tags,
                                                     audio_path.suffix.lstrip(".")))
                else:
                    write_seed_tags(audio_path, SeedTags(
                        title=track.title,
                        artist=track.artist_display or album_artist,
                        albumartist=album_artist,
                        album=lookup.album.title,
                        tracknumber=track.track_number,  # known here, so written
                    ))
                    target = staged / ("%02d - %s%s" % (
                        track.track_number or (index + 1),
                        sanitize_segment(track.title),
                        audio_path.suffix,
                    ))
                    audio_path.rename(target)
                delivered += 1
            except Exception as exc:
                failed.append({"n": track.track_number, "title": track.title,
                               "reason": str(exc)[:200]})
            on_progress((index + 1) / total * 100.0)

        if delivered == 0:
            if only_tracks is not None:
                # Retry pass recovered nothing; the album folder that was
                # already delivered stays exactly as it is.
                already = (library_dir if library
                           else (patch_into if patch_into else config.inbox / folder_name))
                return AlbumGrabOutcome(
                    inbox_path=already,
                    album_title=lookup.album.title,
                    album_artist=album_artist,
                    delivered=0,
                    failed=failed,
                    verified=not (album_level.unverified if library else False),
                )
            raise DownloadError(
                "no tracks could be grabbed: " + failures_text(failed[:5])
            )
        on_stage("moving")
        if library:
            # Tracks were filed as they finished; add the folder art.
            if cover:
                write_cover_file(library_dir, cover[0], cover[1])
            destination = library_dir
        elif patch_into is not None and patch_into.is_dir():
            # Patch recovered tracks into the still-waiting album folder,
            # one atomic replace per file, never overwriting.
            for produced in sorted(staged.iterdir()):
                target = patch_into / produced.name
                if target.exists():
                    continue
                temp = patch_into / (".%s.beetdrop-tmp" % produced.name)
                try:
                    shutil.copyfile(produced, temp)
                    os.replace(temp, target)
                finally:
                    if temp.exists():
                        temp.unlink()
            destination = patch_into
        else:
            # Folder already imported (or first run): deliver as a normal
            # album folder; beets merges retried tracks into the same
            # release on import.
            destination = deliver(staged, config.inbox, folder_name, cross_fs)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    return AlbumGrabOutcome(
        inbox_path=destination,
        album_title=lookup.album.title,
        album_artist=album_artist,
        delivered=delivered,
        failed=failed,
        verified=not (album_level.unverified if library else False),
    )
