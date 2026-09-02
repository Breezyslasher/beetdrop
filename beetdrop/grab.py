"""One grab, end to end: download, match against MusicBrainz, write full
tags and cover art, and file straight into the music library.

Grabs that cannot be verified are filed under _review/ with
YouTube-derived tags and an unverified marker - the clean library never
gets polluted silently.

Stage and progress callbacks exist so the web layer's job queue can
mirror the pipeline; the CLI passes simple printers.
"""

from __future__ import annotations

import random
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional

from .config import Config
from .download import DownloadError, download_audio, download_video
from .fulltags import FullTags, verify_audio, write_full_tags
from .library import (
    album_dir,
    place_file,
    resolve_album,
    resolve_track,
    track_filename,
    write_cover_file,
    write_lyrics_sidecar,
)
from .lyrics import fetch_synced_lyrics
from .mb import MusicBrainzClient
from .search import AlbumLookup, Result, lookup_album, lookup_video
from .videos import video_dir, video_filename, write_nfo, write_poster

# Stage labels, in pipeline order. "queued", "done" and "failed" are owned
# by the job layer.
STAGES = ("searching", "downloading", "extracting", "matching", "tagging", "moving")


@dataclass
class GrabOutcome:
    inbox_path: Path  # final library path (column name kept for history)
    result: Result
    verified: bool = True  # False means filed under _review


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


def _maybe_write_lyrics(config: Config, final_path: Path, tags: FullTags,
                        duration_seconds) -> None:
    """Fetch synced lyrics and drop a .lrc sidecar. Best effort - a
    failure or absent lyrics never affects the grab."""
    if not config.lyrics_enabled:
        return
    try:
        lrc = fetch_synced_lyrics(
            tags.artist, tags.title, tags.album, duration_seconds,
            musixmatch_token=config.musixmatch_token,
            provider=config.lyrics_provider)
        if lrc:
            write_lyrics_sidecar(final_path, lrc)
    except Exception:
        pass


def _finish_into_library(config: Config, audio_path: Path, tags: FullTags,
                         cover, duration_seconds=None) -> Path:
    """Tag fully and file one track into the music library (or _review),
    with a synced-lyrics sidecar when available."""
    cover_bytes, cover_mime = (cover[0], cover[1]) if cover else (None, "image/jpeg")
    write_full_tags(audio_path, tags, cover_bytes, cover_mime)
    directory = album_dir(config.music_root, tags)
    final = place_file(audio_path, directory / track_filename(
        tags, audio_path.suffix.lstrip(".")))
    if cover_bytes and not tags.unverified:
        write_cover_file(directory, cover_bytes, cover_mime)
    _maybe_write_lyrics(config, final, tags, duration_seconds)
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
        final = _finish_into_library(config, audio_path, tags, cover,
                                     result.duration_seconds)
        return GrabOutcome(inbox_path=final, result=result,
                           verified=resolution.matched)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


@dataclass
class VideoGrabOutcome:
    inbox_path: Path  # final video file path (column name kept for history)
    result: Result
    verified: bool = True  # False means no MusicBrainz match (still filed)


def _finish_video(config: Config, video_path: Path, tags: FullTags,
                  cover, duration_seconds=None) -> Path:
    """Embed basic metadata, file the video Kodi-style, and drop the .nfo
    and -poster.jpg sidecars beside it."""
    cover_bytes, cover_mime = (cover[0], cover[1]) if cover else (None, "image/jpeg")
    try:
        write_full_tags(video_path, tags, cover_bytes, cover_mime)
    except Exception:
        # A container mutagen cannot tag still files fine; the NFO carries
        # the metadata Kodi actually reads.
        pass
    directory = video_dir(config.video_root, tags)
    final = place_file(video_path, directory / video_filename(tags))
    write_nfo(final, tags, duration_seconds)
    if cover_bytes:
        write_poster(final, cover_bytes, cover_mime)
    return final


def run_video_grab(
    video_id: str,
    config: Config,
    max_height: int = 0,
    on_stage: Callable[[str], None] = _noop,
    on_progress: Callable[[float], None] = _noop,
    on_resolved: Callable[[Result], None] = _noop,
    logger=None,
) -> VideoGrabOutcome:
    """Grab one music video: download video+audio merged to mp4, best-effort
    match against MusicBrainz for a clean artist/title/album, and file it
    into the video library with a Kodi .nfo and poster. Unlike an audio
    grab, a video that does not match MusicBrainz is still filed (a video
    is the point); the match only enriches its metadata."""
    max_height = max_height or config.video_max_height

    job_id = uuid.uuid4().hex[:12]
    scratch = config.scratch_root / job_id
    config.scratch_root.mkdir(parents=True, exist_ok=True)

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

    try:
        on_stage("downloading")
        video_path = download_video(
            video_id, scratch, max_height=max_height,
            cookies_file=config.cookies_file,
            progress_hook=progress_hook, logger=logger)
        verify_audio(video_path)

        on_stage("matching")
        mb = get_mb_client(config)
        resolution = resolve_track(
            mb, result.title, result.primary_artist, result.duration_seconds)
        if resolution.matched:
            tags = resolution.tags
        else:
            # No MB match: keep the YouTube identity, no invented album.
            tags = FullTags(
                title=result.title,
                artist=result.primary_artist or "Unknown Artist",
                album_artist=result.primary_artist or "Unknown Artist")
        cover = mb.fetch_cover(
            release_mbid=tags.release_mbid,
            release_group_mbid=tags.release_group_mbid,
            thumbnail_url=result.thumbnail_url)

        on_stage("tagging")
        on_stage("moving")
        final = _finish_video(config, video_path, tags, cover,
                              result.duration_seconds)
        return VideoGrabOutcome(inbox_path=final, result=result,
                                verified=resolution.matched)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


@dataclass
class AlbumGrabOutcome:
    inbox_path: Path  # the album directory in the library
    album_title: str
    album_artist: str
    delivered: int
    # {"n": track_number, "title", "reason"} per track that could not be
    # grabbed - structured so a later retry can target exactly these.
    failed: list[dict]
    verified: bool = True  # False means filed under _review


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
) -> AlbumGrabOutcome:
    """Grab a whole album: matched as one MusicBrainz release, every
    playable track downloaded, fully tagged with its track number, and
    filed into one album folder with cover art.

    Individual track failures do not abort the album; they are collected
    and reported. Only an album with zero successful tracks fails.

    Retry mode: only_tracks limits the run to those track numbers;
    recovered files join the existing album folder (collisions are never
    overwritten). Zero recoveries is then a result, not an error.
    """
    fmt = fmt or config.output_format
    bitrate = bitrate or config.bitrate

    job_id = uuid.uuid4().hex[:12]
    scratch = config.scratch_root / job_id
    config.scratch_root.mkdir(parents=True, exist_ok=True)

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

    failed = [
        {"n": u.get("n"), "title": u.get("title", "?"),
         "reason": "not available on YouTube Music"}
        for u in unavailable
    ]
    total = len(tracks)
    delivered = 0

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
                final = place_file(
                    audio_path,
                    library_dir / track_filename(tags,
                                                 audio_path.suffix.lstrip(".")))
                _maybe_write_lyrics(config, final, tags, track.duration_seconds)
                delivered += 1
            except Exception as exc:
                failed.append({"n": track.track_number, "title": track.title,
                               "reason": str(exc)[:200]})
            on_progress((index + 1) / total * 100.0)

        if delivered == 0:
            if only_tracks is not None:
                # Retry pass recovered nothing; whatever was already filed
                # stays exactly as it is.
                return AlbumGrabOutcome(
                    inbox_path=library_dir,
                    album_title=lookup.album.title,
                    album_artist=album_artist,
                    delivered=0,
                    failed=failed,
                    verified=not album_level.unverified,
                )
            raise DownloadError(
                "no tracks could be grabbed: " + failures_text(failed[:5])
            )
        on_stage("moving")
        if cover:
            write_cover_file(library_dir, cover[0], cover[1])
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    return AlbumGrabOutcome(
        inbox_path=library_dir,
        album_title=lookup.album.title,
        album_artist=album_artist,
        delivered=delivered,
        failed=failed,
        verified=not album_level.unverified,
    )
