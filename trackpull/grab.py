"""One grab, end to end: download, seed-tag, verify, atomic inbox handoff.

The success outcome is "handed off to the inbox", not "imported" —
whether and how the file imports is beets' decision, and Trackpull does
not know.

Stage and progress callbacks exist so the web layer's job queue can
mirror the pipeline; the CLI passes simple printers.
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
