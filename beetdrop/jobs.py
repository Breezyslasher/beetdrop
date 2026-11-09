"""Background job execution.

Downloads run in a dedicated thread pool capped at 2 workers; excess jobs
sit in its queue at stage "queued". Searches never touch this pool, so
search never blocks on a download. Worker threads report through the
event loop into the SSE broadcaster.
"""

from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from .backfill import backfill_lyrics
from .companion import grab_companion
from .config import Config, check_storage
from .db import Store
from .download import LogCollector, download_video
from .events import Broadcaster
from .grab import failures_text, run_album_grab, run_grab, run_video_grab
from .plex import scan_folder
from .search import search_videos

DEFAULT_CONCURRENCY = 2
PROGRESS_MIN_INTERVAL = 0.5  # seconds between persisted progress updates


class JobCancelled(Exception):
    """Raised inside a worker when the user cancels the job; propagates
    out through yt-dlp's hooks and the pipeline's callbacks."""

MAINTENANCE_INTERVAL = 60  # seconds between history-pruning passes


class JobManager:
    def __init__(self, store: Store, broadcaster: Broadcaster,
                 config_provider: Callable[[], Config],
                 max_workers: int = DEFAULT_CONCURRENCY):
        self._store = store
        self._broadcaster = broadcaster
        self._config_provider = config_provider
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, min(4, max_workers)), thread_name_prefix="grab"
        )
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._sweep_task: Optional[asyncio.Task] = None
        self._cancel_requested: set = set()

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        # Jobs that never started can simply run now; jobs that were
        # mid-flight when the process died cannot finish and are surfaced
        # honestly instead of showing them stuck.
        for job in self._store.interrupted_jobs():
            if job["stage"] == "queued":
                self._executor.submit(self._run, job["id"])
            else:
                self._update(job["id"], stage="failed",
                             error="interrupted by restart; retry to run again")
        self._sweep_task = self._loop.create_task(self._sweep_loop())

    def shutdown(self) -> None:
        if self._sweep_task is not None:
            self._sweep_task.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(MAINTENANCE_INTERVAL)
            try:
                config = self._config_provider()
                self._store.prune(config.keep_jobs, config.keep_days)
            except Exception:
                pass  # a failed pass just means the next one tries again

    def active_count(self) -> int:
        return sum(
            1 for job in self._store.list_jobs()
            if job["stage"] not in ("done", "failed", "cancelled")
        )

    def cancel(self, job_id: str) -> Optional[dict]:
        """Request cancellation. Queued jobs cancel before starting;
        running jobs stop at their next progress or stage callback."""
        job = self._store.get_job(job_id)
        if job is None:
            return None
        if job["stage"] in ("done", "failed", "cancelled"):
            return job
        self._cancel_requested.add(job_id)
        return job

    def _checkpoint(self, job_id: str) -> None:
        if job_id in self._cancel_requested:
            raise JobCancelled()

    def enqueue(self, video_id: str, fmt: str = "", bitrate: str = "",
                kind: str = "track", with_video: bool = False) -> dict:
        config = self._config_provider()
        job = self._store.create_job(
            video_id, fmt or config.output_format, bitrate or config.bitrate,
            kind=kind, with_video=with_video and kind == "track",
        )
        self._publish(job)
        self._executor.submit(self._run, job["id"])
        return job

    def retry(self, job_id: str) -> Optional[dict]:
        job = self._store.get_job(job_id)
        if job is None:
            return None
        if job["stage"] in ("failed", "cancelled"):
            job = self._update(job_id, stage="queued", progress=0.0, error="",
                               detail="", log="", failed_tracks="",
                               inbox_path="", inbox_state="")
            self._executor.submit(self._run, job_id)
            return job
        if (job["stage"] == "done" and job["kind"] == "album"
                and job["failed_tracks"]):
            # Retry only the tracks that failed; failed_tracks stays so
            # the run knows which track numbers to fetch. Recovered files
            # join the existing album folder.
            job = self._update(job_id, stage="queued", progress=0.0,
                               error="", detail="", log="")
            self._executor.submit(self._run, job_id)
            return job
        return job

    # -- worker thread side --------------------------------------------------

    def _run(self, job_id: str) -> None:
        job = self._store.get_job(job_id)
        if job is None:
            return
        if job_id in self._cancel_requested:
            # Cancelled while still queued.
            self._cancel_requested.discard(job_id)
            self._update(job_id, stage="cancelled", error="cancelled before start")
            return
        config = self._config_provider()
        last_progress = {"at": 0.0, "value": -1.0}
        collector = LogCollector()

        def on_stage(stage: str) -> None:
            self._checkpoint(job_id)
            collector.add("stage: %s" % stage)
            self._update(job_id, stage=stage)

        def on_progress(pct: float) -> None:
            self._checkpoint(job_id)
            now = time.monotonic()
            if (now - last_progress["at"] < PROGRESS_MIN_INTERVAL
                    and pct - last_progress["value"] < 5.0 and pct < 100.0):
                return
            last_progress["at"] = now
            last_progress["value"] = pct
            self._update(job_id, progress=round(pct, 1))

        def on_detail(text: str) -> None:
            self._checkpoint(job_id)
            collector.add(text)
            self._update(job_id, detail=text)

        def audio_state(verified: bool) -> str:
            return "filed" if verified else "unverified"

        try:
            if job["kind"] == "lyricscan":
                # Backfill synced lyrics across the whole library. Not a
                # download; it reads the library it also writes .lrc into.
                check_storage(config.music_root, config.min_free_mb)
                # video_id carries the mode: "__refresh__" also purges
                # placeholder sidecars before re-fetching.
                purge = job["video_id"] == "__refresh__"
                upgrade = job["video_id"] == "__upgrade__"
                redo = job["video_id"] == "__rewords__"
                self._update(job_id, stage="scanning", title=(
                    "Library word-by-word re-render" if redo else
                    "Library lyrics upgrade" if upgrade else
                    "Library lyrics refresh" if purge else "Library lyrics scan"))
                result = backfill_lyrics(config, on_progress=on_progress,
                                         on_detail=on_detail, purge_bad=purge,
                                         upgrade=upgrade, redo_words=redo,
                                         store=self._store)
                if redo:
                    detail = "re-rendered %d of %d word-by-word tracks" % (
                        result.upgraded, result.total)
                    if result.no_match:
                        detail += " (%d left as they were)" % result.no_match
                elif upgrade:
                    detail = "upgraded %d of %d line-level tracks to word-by-word" % (
                        result.upgraded, result.total)
                    if result.no_match:
                        detail += " (%d had no word-level version)" % result.no_match
                else:
                    detail = "added lyrics to %d of %d tracks missing them" % (
                        result.added, result.total)
                    extras = []
                    if result.purged:
                        extras.append("%d placeholder files removed first" % result.purged)
                    if result.no_match:
                        extras.append("%d with no synced lyrics found" % result.no_match)
                    if result.skipped:
                        extras.append("%d skipped (no title/artist)" % result.skipped)
                    if extras:
                        detail += " (" + ", ".join(extras) + ")"
                if result.deferred:
                    # These were never answered, so the pass is incomplete.
                    # Reporting a plain "done" would hide a bad run.
                    detail += ("; %d could not be checked (rate limit, token, "
                               "or network) - run it again to retry them"
                               % result.deferred)
                self._update(job_id, stage="done", progress=100.0,
                             detail=detail[:2000], log=collector.text()[:20000])
                if result.added or result.upgraded or result.purged:
                    self._tell_plex(job_id, config, config.music_root)
                return
            if job["kind"] == "videoart":
                # Embed the image already beside each music video into it.
                # Reads and rewrites the library; downloads nothing.
                from .videoart import backfill_video_art
                self._update(job_id, stage="scanning",
                             title="Embed music video artwork")
                result = backfill_video_art(
                    [config.music_root, config.video_root],
                    on_progress=on_progress, on_detail=on_detail)
                detail = "embedded artwork in %d of %d music videos" % (
                    result.embedded, result.total)
                extras = []
                if result.already:
                    extras.append("%d already had it" % result.already)
                if result.no_image:
                    extras.append("%d with no image beside them" % result.no_image)
                if result.unusable:
                    extras.append("%d whose image is not JPEG or PNG" % result.unusable)
                if result.failed:
                    top, count = result.reasons.most_common(1)[0]
                    extras.append("%d could not be read or written%s" % (
                        result.failed, " - %d: %s" % (count, top)
                        if count != result.failed else ": " + top))
                if extras:
                    detail += " (" + ", ".join(extras) + ")"
                if result.by_prefix:
                    detail += "; %d matched to an image with a shortened name" % (
                        result.by_prefix)
                if result.no_image:
                    detail += "; the log lists examples with no image"
                # The report first: the per-file lines can run past the
                # log's length limit, and the report is the useful part.
                report = result.report()
                log = (report + "\n\n" if report else "") + collector.text()
                self._update(job_id, stage="done", progress=100.0,
                             detail=detail[:2000], log=log[:20000])
                return
            if job["kind"] == "musicvideo":
                # The video root is a subfolder we create on first use; make
                # it before the writability check so a fresh install works.
                config.video_root.mkdir(parents=True, exist_ok=True)
                check_storage(config.video_root, config.min_free_mb)
                outcome = run_video_grab(
                    job["video_id"], config,
                    on_stage=on_stage, on_progress=on_progress,
                    on_resolved=lambda result: self._update(
                        job_id, title=result.title, artist=result.artist_display),
                    logger=collector,
                )
                self._update(job_id, stage="done", progress=100.0,
                             log=collector.text()[:20000],
                             inbox_path=str(outcome.inbox_path),
                             inbox_state=audio_state(outcome.verified))
                self._tell_plex(job_id, config, outcome.inbox_path.parent)
                return
            check_storage(config.music_root, config.min_free_mb)
            if job["kind"] == "album":
                only_tracks = None
                if job["failed_tracks"]:
                    # This run is a targeted retry of previously failed
                    # tracks; recovered files join the album folder.
                    try:
                        previous = json.loads(job["failed_tracks"])
                    except ValueError:
                        previous = []
                    only_tracks = {f.get("n") for f in previous if f.get("n")}
                outcome = run_album_grab(
                    job["video_id"], config,
                    fmt=job["format"], bitrate=job["bitrate"],
                    on_stage=on_stage, on_progress=on_progress,
                    on_resolved=lambda title, artist: self._update(
                        job_id, title=title, artist=artist),
                    on_detail=on_detail,
                    logger=collector,
                    only_tracks=only_tracks,
                )
                if only_tracks is not None:
                    detail = "retry recovered %d of %d failed tracks" % (
                        outcome.delivered, len(only_tracks))
                else:
                    total = outcome.delivered + len(outcome.failed)
                    detail = "delivered %d/%d tracks" % (outcome.delivered, total)
                if outcome.failed:
                    detail += "; still failing: " if only_tracks is not None else "; failed: "
                    detail += failures_text(outcome.failed)
                self._update(job_id, stage="done", progress=100.0,
                             detail=detail[:2000], log=collector.text()[:20000],
                             failed_tracks=json.dumps(outcome.failed) if outcome.failed else "",
                             inbox_path=str(outcome.inbox_path),
                             inbox_state=audio_state(outcome.verified))
                # One scan for the album folder, not one per track.
                if outcome.delivered:
                    self._tell_plex(job_id, config, outcome.inbox_path)
            else:
                outcome = run_grab(
                    job["video_id"], config,
                    fmt=job["format"], bitrate=job["bitrate"],
                    on_stage=on_stage, on_progress=on_progress,
                    on_resolved=lambda result: self._update(
                        job_id, title=result.title, artist=result.artist_display),
                    logger=collector,
                )
                detail = outcome.note
                if job.get("with_video"):
                    video = self._companion(job_id, config, outcome,
                                            on_stage, on_progress, collector)
                    detail = "; ".join(part for part in (detail, video) if part)
                self._update(job_id, stage="done", progress=100.0,
                             detail=detail[:2000],
                             log=collector.text()[:20000],
                             inbox_path=str(outcome.inbox_path),
                             inbox_state=audio_state(outcome.verified))
                # The album folder, which also holds a companion video.
                self._tell_plex(job_id, config, outcome.inbox_path.parent)
        except JobCancelled:
            collector.add("cancelled by user")
            self._update(job_id, stage="cancelled", error="cancelled by user",
                         log=collector.text()[:20000])
        except Exception as exc:
            if job_id in self._cancel_requested:
                # yt-dlp can wrap the cancellation raised inside its
                # hooks; a failure while cancellation was requested is a
                # cancellation, not an error.
                collector.add("cancelled by user")
                self._update(job_id, stage="cancelled", error="cancelled by user",
                             log=collector.text()[:20000])
            else:
                collector.add("ERROR: %s" % exc)
                self._update(job_id, stage="failed", error=str(exc)[:2000],
                             log=collector.text()[:20000])
        finally:
            self._cancel_requested.discard(job_id)

    def _tell_plex(self, job_id: str, config, folder) -> None:
        """Ask Plex to scan what this job just filed, and add how that
        went to the job card.

        After the job is already marked done: the grab succeeded whatever
        Plex says, and a server that is slow to answer must not leave a
        finished grab looking unfinished.
        """
        try:
            line = scan_folder(config, folder)
        except Exception as exc:
            line = "Plex was not told: %s" % exc
        if not line:
            return
        job = self._store.get_job(job_id) or {}
        detail = job.get("detail") or ""
        self._update(job_id, detail=((detail + "; ") if detail else "") + line)

    def _companion(self, job_id, config, outcome, on_stage, on_progress,
                   collector) -> str:
        """Fetch the track's music video and file it beside the track.

        Runs after the audio is filed, so whatever happens here the grab
        that was asked for has already succeeded. Returns one line for the
        job card saying what became of the video.
        """
        # A track sent to _review gets its video too: the video is named
        # for the audio file, not the title, and fixing the match later
        # carries it along (rematch.apply_choice -> carry_companion).
        on_stage("video")
        self._update(job_id, progress=0.0)
        found = grab_companion(config, outcome.inbox_path, outcome.tags,
                               outcome.result, search=search_videos,
                               download=download_video,
                               on_progress=on_progress, logger=collector)
        collector.add(found.detail)
        return found.detail

    # -- plumbing --------------------------------------------------------------

    def _update(self, job_id: str, **fields) -> Optional[dict]:
        job = self._store.update_job(job_id, **fields)
        if job is not None:
            self._publish(job)
        return job

    def _publish(self, job: dict) -> None:
        if self._loop is None or self._loop.is_closed():
            return
        try:
            self._loop.call_soon_threadsafe(self._broadcaster.publish, job)
        except RuntimeError:
            pass  # loop shut down mid-job
