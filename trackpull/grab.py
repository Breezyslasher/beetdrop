"""One grab, end to end: download, seed-tag, verify, atomic inbox handoff.

The success outcome is "handed off to the inbox", not "imported" —
whether and how the file imports is beets' decision, and Trackpull does
not know.

Stage and progress callbacks exist so the web layer's job queue can
mirror the pipeline; the CLI passes simple printers.
"""

from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .config import Config
from .download import DownloadError, download_audio
from .handoff import deliver, same_filesystem
from .paths import grab_folder_name, sanitize_segment
from .search import AlbumLookup, Result, lookup_album, lookup_video
from .seedtags import SeedTags, seed_from_result, verify_audio, write_seed_tags

# Stage labels, in pipeline order. "queued", "done" and "failed" are owned
# by the job layer.
STAGES = ("searching", "downloading", "extracting", "tagging", "moving")


@dataclass
class GrabOutcome:
    inbox_path: Path
    result: Result


def _noop(*args) -> None:
    pass


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

        on_stage("tagging")
        verify_audio(audio_path)
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

    try:
        staged = scratch / "staged" / folder_name
        staged.mkdir(parents=True)
        on_stage("downloading")
        for index, track in enumerate(tracks):
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
                return AlbumGrabOutcome(
                    inbox_path=patch_into if patch_into else config.inbox / folder_name,
                    album_title=lookup.album.title,
                    album_artist=album_artist,
                    delivered=0,
                    failed=failed,
                )
            raise DownloadError(
                "no tracks could be grabbed: " + failures_text(failed[:5])
            )
        on_stage("moving")
        if patch_into is not None and patch_into.is_dir():
            # Patch recovered tracks into the still-waiting album folder,
            # one atomic replace per file, never overwriting.
            for produced in sorted(staged.iterdir()):
                target = patch_into / produced.name
                if target.exists():
                    continue
                temp = patch_into / (".%s.trackpull-tmp" % produced.name)
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
    )
