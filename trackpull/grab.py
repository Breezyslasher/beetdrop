"""One grab, end to end: download, seed-tag, verify, atomic inbox handoff.

The success outcome is "handed off to the inbox", not "imported" —
whether and how the file imports is beets' decision, and Trackpull does
not know.
"""

from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .config import Config
from .download import download_audio
from .handoff import deliver, same_filesystem
from .paths import grab_folder_name
from .search import Result, lookup_video
from .seedtags import seed_from_result, verify_audio, write_seed_tags


@dataclass
class GrabOutcome:
    inbox_path: Path
    result: Result


def _log_default(message: str) -> None:
    print(message)


def run_grab(
    video_id: str,
    config: Config,
    fmt: str = "",
    bitrate: str = "",
    log: Callable[[str], None] = _log_default,
    progress_hook: Optional[Callable[[dict], None]] = None,
) -> GrabOutcome:
    fmt = fmt or config.output_format
    bitrate = bitrate or config.bitrate

    job_id = uuid.uuid4().hex[:12]
    scratch = config.scratch_root / job_id
    cross_fs_probe = config.scratch_root
    cross_fs_probe.mkdir(parents=True, exist_ok=True)
    cross_fs = not same_filesystem(cross_fs_probe, config.inbox)

    log("stage: searching")
    result = lookup_video(video_id)
    log("resolved: %s - %s (%ss)" % (result.artist_display or "?", result.title, result.duration_seconds))

    try:
        log("stage: downloading")
        audio_path = download_audio(
            video_id, scratch, fmt=fmt, bitrate=bitrate,
            cookies_file=config.cookies_file, progress_hook=progress_hook,
        )

        log("stage: tagging")
        verify_audio(audio_path)
        seed = seed_from_result(result)
        write_seed_tags(audio_path, seed)

        log("stage: moving")
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

    log("done: handed off to %s" % destination)
    return GrabOutcome(inbox_path=destination, result=result)
