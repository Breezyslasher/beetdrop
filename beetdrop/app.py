"""FastAPI application.

Endpoints:

    GET  /api/search?q=&limit=        songs-filtered search, normalised
    POST /api/grab                    {video_id, format, bitrate}
    GET  /api/jobs                    recent jobs from SQLite
    POST /api/jobs/{id}/retry
    GET  /api/settings
    PUT  /api/settings
    GET  /api/health                  library writability and yt-dlp version
    GET  /events                      SSE stream of job state changes

Auth is an optional single shared password, LAN-tool grade: when set,
requests carry it in an X-Beetdrop-Password header or a password query
parameter (EventSource cannot set headers). /api/health stays open so
container healthchecks work.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, musixmatch, updater
from .auth import (
    LoginThrottle,
    check_session_token,
    hash_password,
    is_hashed,
    load_or_create_secret,
    make_session_token,
    verify_password,
)
from .config import SUPPORTED_FORMATS, Config, storage_free_mb, storage_problem
from .db import Store
from .download import ytdlp_version
from .events import Broadcaster, sse_format
from .jobs import JobManager
from .search import search_albums, search_songs, search_videos

SSE_KEEPALIVE_SECONDS = 15
STATIC_DIR = Path(__file__).parent / "static"


class GrabRequest(BaseModel):
    video_id: str  # a videoId for kind=track, an album browseId for kind=album
    kind: str = "track"
    format: str = ""
    bitrate: str = ""
    force: bool = False  # grab even when it was already grabbed


class SettingsUpdate(BaseModel):
    output_format: Optional[str] = None
    bitrate: Optional[str] = None
    password: Optional[str] = None
    concurrency: Optional[int] = None
    cookies: Optional[str] = None  # cookies.txt content; "" clears
    music_root: Optional[str] = None
    lyrics: Optional[bool] = None
    lyrics_provider: Optional[str] = None  # "lrclib" | "musixmatch"
    musixmatch_token: Optional[str] = None  # "" clears
    video_root: Optional[str] = None
    video_max_height: Optional[int] = None


class LoginRequest(BaseModel):
    password: str


SESSION_COOKIE = "beetdrop_session"


def create_app(base_config: Optional[Config] = None) -> FastAPI:
    base = base_config or Config()
    store = Store(base.db_path)
    broadcaster = Broadcaster()

    uploaded_cookies = base.config_dir / "cookies.txt"
    # When MUSIC_PATH is set (always true in the Docker image), the library
    # location is the container mount and must NOT be editable from the UI:
    # setting an unmounted host path there just breaks writes. The env value
    # then wins over any stored override.
    music_locked = "MUSIC_PATH" in os.environ
    # Same reasoning for the video library: when VIDEO_PATH pins it to a
    # container mount, the UI must not offer to repoint it off the mount.
    video_locked = "VIDEO_PATH" in os.environ

    def effective_config() -> Config:
        """Environment defaults, overridden by settings stored in SQLite."""
        stored = store.get_settings()
        config = replace(base)
        if stored.get("output_format"):
            config.output_format = stored["output_format"]
        if stored.get("bitrate"):
            config.bitrate = stored["bitrate"]
        if stored.get("password"):
            config.password = stored["password"]
        if stored.get("concurrency"):
            try:
                config.concurrency = int(stored["concurrency"])
            except ValueError:
                pass
        if stored.get("music_root") and not music_locked:
            config.music_root = Path(stored["music_root"])
        if stored.get("lyrics") in ("0", "1"):
            config.lyrics_enabled = stored["lyrics"] == "1"
        if stored.get("mxm_token"):
            config.musixmatch_token = stored["mxm_token"]
        if stored.get("lyrics_provider") in ("lrclib", "musixmatch"):
            config.lyrics_provider = stored["lyrics_provider"]
        if stored.get("video_root") and not video_locked:
            config.video_root = Path(stored["video_root"])
        if stored.get("video_max_height"):
            try:
                config.video_max_height = int(stored["video_max_height"])
            except ValueError:
                pass
        # Cookies uploaded through Settings win over the mounted file.
        if uploaded_cookies.is_file() and uploaded_cookies.stat().st_size > 0:
            config.cookies_file = str(uploaded_cookies)
        return config

    secret = load_or_create_secret(base.config_dir)
    throttle = LoginThrottle()
    # A yt-dlp updated through the UI persists in /config; load it now
    # if it is at least as new as the bundled copy.
    try:
        updater.activate(base.config_dir)
    except Exception as exc:
        print("WARNING: could not activate persisted yt-dlp update: %s" % exc)
    # Worker pool size comes from settings at startup; changing the
    # setting applies on the next restart.
    manager = JobManager(store, broadcaster, effective_config,
                         max_workers=effective_config().concurrency)

    # A plaintext password stored by an earlier version is hashed in
    # place on startup; it never needs to exist in plaintext again.
    stored_settings = store.get_settings()
    if stored_settings.get("password") and not is_hashed(stored_settings["password"]):
        store.set_settings({"password": hash_password(stored_settings["password"])})

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        manager.start()
        problem = storage_problem(effective_config().music_root)
        if problem:
            # Loud at startup, and again in /api/health; the process still
            # serves so the problem is visible over HTTP, not just in logs.
            print("WARNING: %s" % problem)
        yield
        manager.shutdown()
        store.close()

    app = FastAPI(title="beetdrop", version=__version__, lifespan=lifespan)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    def client_key(request: Request) -> str:
        # Behind a Cloudflare tunnel every connection arrives from
        # cloudflared, so the real client is in CF-Connecting-IP.
        return (
            request.headers.get("cf-connecting-ip")
            or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "unknown")
        )

    async def require_password(request: Request) -> None:
        password = effective_config().password
        if not password:
            return
        cookie = request.cookies.get(SESSION_COOKIE, "")
        if cookie and check_session_token(cookie, secret, password):
            return
        # Header auth for scripts and curl. Failed attempts count toward
        # the same throttle as login attempts. The password is never
        # accepted in a query string - query strings end up in logs.
        # The pre-rename header name is still accepted.
        header = (request.headers.get("x-beetdrop-password", "")
                  or request.headers.get("x-trackpull-password", ""))
        if header:
            key = client_key(request)
            if throttle.retry_after(key):
                raise HTTPException(status_code=429, detail="too many attempts; try later")
            if verify_password(header, password):
                throttle.record_success(key)
                return
            throttle.record_failure(key)
        raise HTTPException(status_code=401, detail="password required")

    protected = Depends(require_password)

    @app.post("/api/login")
    async def api_login(body: LoginRequest, request: Request, response: Response):
        password = effective_config().password
        if not password:
            return {"ok": True, "password_required": False}
        key = client_key(request)
        wait = throttle.retry_after(key)
        if wait:
            raise HTTPException(
                status_code=429,
                detail="too many attempts; try again in %d seconds" % wait,
                headers={"Retry-After": str(wait)},
            )
        if not verify_password(body.password, password):
            throttle.record_failure(key)
            raise HTTPException(status_code=401, detail="wrong password")
        throttle.record_success(key)
        forwarded_proto = request.headers.get("x-forwarded-proto", "")
        response.set_cookie(
            SESSION_COOKIE,
            make_session_token(secret, password),
            max_age=30 * 86400,
            httponly=True,
            samesite="lax",
            secure=(request.url.scheme == "https" or forwarded_proto == "https"),
        )
        return {"ok": True, "password_required": True}

    # -- endpoints -----------------------------------------------------------

    @app.get("/api/search", dependencies=[protected])
    async def api_search(q: str, limit: int = 8, type: str = "songs"):
        if type not in ("songs", "albums", "videos"):
            raise HTTPException(status_code=422,
                                detail="type must be songs, albums, or videos")
        limit = max(1, min(limit, 20))
        # to_thread keeps the loop free; downloads run in their own pool,
        # so a search never waits behind one.
        search = {"albums": search_albums, "videos": search_videos}.get(
            type, search_songs)
        results = await asyncio.to_thread(search, q, limit)
        return {"type": type, "results": [asdict(r) for r in results]}

    @app.post("/api/grab", status_code=202, dependencies=[protected])
    async def api_grab(body: GrabRequest):
        if body.format and body.format not in SUPPORTED_FORMATS:
            raise HTTPException(status_code=422, detail="format must be one of %s" % (SUPPORTED_FORMATS,))
        if body.kind not in ("track", "album", "musicvideo"):
            raise HTTPException(status_code=422,
                                detail="kind must be track, album, or musicvideo")
        if not body.video_id.strip():
            raise HTTPException(status_code=422, detail="video_id is required")
        video_id = body.video_id.strip()
        if not body.force:
            duplicate = store.find_duplicate(video_id, body.kind)
            if duplicate is not None:
                active = duplicate["stage"] not in ("done",)
                raise HTTPException(status_code=409, detail={
                    "message": ("this %s is already being grabbed" % body.kind)
                               if active else
                               ("this %s was already grabbed" % body.kind),
                    "existing_job": {k: duplicate[k] for k in
                                     ("id", "title", "artist", "stage", "created_at")},
                })
        job = manager.enqueue(video_id, body.format, body.bitrate, kind=body.kind)
        return job

    @app.get("/api/jobs", dependencies=[protected])
    async def api_jobs(limit: int = 50):
        return {"jobs": store.list_jobs(max(1, min(limit, 200)))}

    @app.post("/api/jobs/{job_id}/retry", dependencies=[protected])
    async def api_retry(job_id: str):
        job = manager.retry(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such job")
        return job

    @app.post("/api/jobs/{job_id}/cancel", dependencies=[protected])
    async def api_cancel(job_id: str):
        job = manager.cancel(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such job")
        return job

    @app.get("/api/settings", dependencies=[protected])
    async def api_get_settings():
        config = effective_config()
        return {
            "output_format": config.output_format,
            "bitrate": config.bitrate,
            "password_set": bool(config.password),
            "concurrency": config.concurrency,
            "workers_active": True,  # concurrency changes apply on restart
            "cookies_set": bool(config.cookies_file),
            "music_root": str(config.music_root),
            "music_root_locked": music_locked,  # env-controlled; hide field
            "lyrics": config.lyrics_enabled,
            "lyrics_provider": config.lyrics_provider,
            "musixmatch_token_set": bool(config.musixmatch_token),
            "video_root": str(config.video_root),
            "video_root_locked": video_locked,  # env-controlled; hide field
            "video_max_height": config.video_max_height,
            "ytdlp_version": ytdlp_version(),  # read-only
        }

    @app.put("/api/settings", dependencies=[protected])
    async def api_put_settings(body: SettingsUpdate):
        if body.output_format is not None and body.output_format not in SUPPORTED_FORMATS:
            raise HTTPException(status_code=422, detail="format must be one of %s" % (SUPPORTED_FORMATS,))
        if body.concurrency is not None and not (1 <= body.concurrency <= 4):
            raise HTTPException(status_code=422, detail="concurrency must be 1-4")
        if body.music_root is not None and music_locked:
            raise HTTPException(
                status_code=422,
                detail="music library path is set by MUSIC_PATH and cannot be changed here")
        if body.video_root is not None and video_locked:
            raise HTTPException(
                status_code=422,
                detail="video library path is set by VIDEO_PATH and cannot be changed here")
        if body.video_max_height is not None and not (0 <= body.video_max_height <= 4320):
            raise HTTPException(status_code=422,
                                detail="video_max_height must be 0-4320")
        if body.cookies is not None:
            # Stored as a file because yt-dlp wants a cookiefile path;
            # kept out of the DB and readable only by the app user.
            if body.cookies.strip():
                uploaded_cookies.parent.mkdir(parents=True, exist_ok=True)
                uploaded_cookies.touch(mode=0o600, exist_ok=True)
                uploaded_cookies.write_text(body.cookies)
            elif uploaded_cookies.exists():
                uploaded_cookies.unlink()
        if body.lyrics_provider is not None and body.lyrics_provider not in ("lrclib", "musixmatch"):
            raise HTTPException(status_code=422, detail="lyrics_provider must be lrclib or musixmatch")
        updates = {k: v for k, v in body.model_dump().items()
                   if v is not None and k not in ("cookies", "musixmatch_token")}
        if "lyrics" in updates:
            updates["lyrics"] = "1" if updates["lyrics"] else "0"
        if body.musixmatch_token is not None:
            updates["mxm_token"] = body.musixmatch_token.strip()
        if updates.get("password"):
            # Hashed at rest; changing it also invalidates every session,
            # since the hash is part of the token signing key.
            updates["password"] = hash_password(updates["password"])
        store.set_settings(updates)
        return await api_get_settings()

    @app.get("/api/health")
    async def api_health():
        config = effective_config()
        problem = storage_problem(config.music_root, config.min_free_mb)
        return {
            "status": "ok" if not problem else "degraded",
            "library": str(config.music_root),
            "library_writable": not problem,
            "library_problem": problem,
            "library_free_mb": storage_free_mb(config.music_root),
            "min_free_mb": config.min_free_mb,
            "ytdlp_version": ytdlp_version(),
            "version": __version__,
            "active_jobs": manager.active_count(),
            # A wave of these usually means yt-dlp needs updating.
            "failures_last_hour": store.count_failed_since(3600),
        }

    @app.post("/api/ytdlp/update", dependencies=[protected])
    async def api_ytdlp_update():
        """Update yt-dlp into the config volume and hot-swap the loaded
        module - active immediately, no restart, and it persists across
        restarts. The server runs unprivileged, so the system copy is
        not touched."""
        try:
            result = await asyncio.to_thread(
                updater.update_and_reload, base.config_dir)
        except Exception as exc:
            raise HTTPException(status_code=502, detail="update failed: %s" % exc)
        return {
            "loaded_version": result["old"],
            "installed_version": result["new"],
            "active": True,
            "restart_needed": False,
        }

    @app.post("/api/lyrics/musixmatch-token", dependencies=[protected])
    async def api_musixmatch_token():
        """Fetch a fresh Musixmatch usertoken and store it - the
        synced-lyrics fallback then works immediately and persists.
        Same shape as the yt-dlp update button."""
        try:
            token = await asyncio.to_thread(musixmatch.fetch_token)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        store.set_settings({"mxm_token": token})
        return {"ok": True, "token_set": True}

    @app.get("/events", dependencies=[protected])
    async def events(request: Request):
        queue = broadcaster.subscribe()

        async def stream():
            try:
                yield ": connected\n\n"
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        event = await asyncio.wait_for(
                            queue.get(), timeout=SSE_KEEPALIVE_SECONDS
                        )
                        yield sse_format(event)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                broadcaster.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })

    # -- UI shell ------------------------------------------------------------
    # The shell is unauthenticated by design: the password guards the API,
    # and the page itself is what asks for the password. The service worker
    # must live at the root so its scope covers the whole app.
    #
    # Every shell asset is served Cache-Control: no-cache so browsers
    # revalidate (cheap 304s via ETag) instead of heuristically caching -
    # a stale app.js against a fresh index.html breaks the UI silently.

    NO_CACHE = {"Cache-Control": "no-cache"}

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html", headers=NO_CACHE)

    @app.get("/manifest.webmanifest", include_in_schema=False)
    async def manifest():
        return FileResponse(STATIC_DIR / "manifest.webmanifest",
                            media_type="application/manifest+json",
                            headers=NO_CACHE)

    @app.get("/sw.js", include_in_schema=False)
    async def service_worker():
        return FileResponse(STATIC_DIR / "sw.js", media_type="text/javascript",
                            headers=NO_CACHE)

    class RevalidatedStaticFiles(StaticFiles):
        def file_response(self, *args, **kwargs):
            response = super().file_response(*args, **kwargs)
            response.headers["Cache-Control"] = "no-cache"
            return response

    app.mount("/static", RevalidatedStaticFiles(directory=str(STATIC_DIR)), name="static")

    return app
