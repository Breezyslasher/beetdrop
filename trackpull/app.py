"""FastAPI application.

Endpoints:

    GET  /api/search?q=&limit=        songs-filtered search, normalised
    POST /api/grab                    {video_id, format, bitrate}
    GET  /api/jobs                    recent jobs from SQLite
    POST /api/jobs/{id}/retry
    GET  /api/settings
    PUT  /api/settings
    GET  /api/health                  inbox writability and yt-dlp version
    GET  /events                      SSE stream of job state changes

Auth is an optional single shared password, LAN-tool grade: when set,
requests carry it in an X-Trackpull-Password header or a password query
parameter (EventSource cannot set headers). /api/health stays open so
container healthchecks work.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__
from .auth import (
    LoginThrottle,
    check_session_token,
    hash_password,
    is_hashed,
    load_or_create_secret,
    make_session_token,
    verify_password,
)
from .config import SUPPORTED_FORMATS, Config, inbox_free_mb, inbox_problem
from .db import Store
from .download import ytdlp_version
from .events import Broadcaster, sse_format
from .jobs import JobManager
from .search import search_albums, search_songs

SSE_KEEPALIVE_SECONDS = 15
STATIC_DIR = Path(__file__).parent / "static"


class GrabRequest(BaseModel):
    video_id: str  # a videoId for kind=track, an album browseId for kind=album
    kind: str = "track"
    format: str = ""
    bitrate: str = ""


class SettingsUpdate(BaseModel):
    output_format: Optional[str] = None
    bitrate: Optional[str] = None
    inbox: Optional[str] = None
    password: Optional[str] = None


class LoginRequest(BaseModel):
    password: str


SESSION_COOKIE = "trackpull_session"


def create_app(base_config: Optional[Config] = None) -> FastAPI:
    base = base_config or Config()
    store = Store(base.db_path)
    broadcaster = Broadcaster()

    def effective_config() -> Config:
        """Environment defaults, overridden by settings stored in SQLite."""
        stored = store.get_settings()
        config = replace(base)
        if stored.get("output_format"):
            config.output_format = stored["output_format"]
        if stored.get("bitrate"):
            config.bitrate = stored["bitrate"]
        if stored.get("inbox"):
            config.inbox = Path(stored["inbox"])
        if stored.get("password"):
            config.password = stored["password"]
        return config

    manager = JobManager(store, broadcaster, effective_config)
    secret = load_or_create_secret(base.config_dir)
    throttle = LoginThrottle()

    # A plaintext password stored by an earlier version is hashed in
    # place on startup; it never needs to exist in plaintext again.
    stored_settings = store.get_settings()
    if stored_settings.get("password") and not is_hashed(stored_settings["password"]):
        store.set_settings({"password": hash_password(stored_settings["password"])})

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        manager.start()
        problem = inbox_problem(effective_config().inbox)
        if problem:
            # Loud at startup, and again in /api/health; the process still
            # serves so the problem is visible over HTTP, not just in logs.
            print("WARNING: %s" % problem)
        yield
        manager.shutdown()
        store.close()

    app = FastAPI(title="trackpull", version=__version__, lifespan=lifespan)

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
        header = request.headers.get("x-trackpull-password", "")
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
        if type not in ("songs", "albums"):
            raise HTTPException(status_code=422, detail="type must be songs or albums")
        limit = max(1, min(limit, 20))
        # to_thread keeps the loop free; downloads run in their own pool,
        # so a search never waits behind one.
        search = search_albums if type == "albums" else search_songs
        results = await asyncio.to_thread(search, q, limit)
        return {"type": type, "results": [asdict(r) for r in results]}

    @app.post("/api/grab", status_code=202, dependencies=[protected])
    async def api_grab(body: GrabRequest):
        if body.format and body.format not in SUPPORTED_FORMATS:
            raise HTTPException(status_code=422, detail="format must be one of %s" % (SUPPORTED_FORMATS,))
        if body.kind not in ("track", "album"):
            raise HTTPException(status_code=422, detail="kind must be track or album")
        if not body.video_id.strip():
            raise HTTPException(status_code=422, detail="video_id is required")
        job = manager.enqueue(body.video_id.strip(), body.format, body.bitrate,
                              kind=body.kind)
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

    @app.get("/api/settings", dependencies=[protected])
    async def api_get_settings():
        config = effective_config()
        return {
            "output_format": config.output_format,
            "bitrate": config.bitrate,
            "inbox": str(config.inbox),
            "password_set": bool(config.password),
            "ytdlp_version": ytdlp_version(),  # read-only
        }

    @app.put("/api/settings", dependencies=[protected])
    async def api_put_settings(body: SettingsUpdate):
        if body.output_format is not None and body.output_format not in SUPPORTED_FORMATS:
            raise HTTPException(status_code=422, detail="format must be one of %s" % (SUPPORTED_FORMATS,))
        updates = {k: v for k, v in body.model_dump().items() if v is not None}
        if updates.get("password"):
            # Hashed at rest; changing it also invalidates every session,
            # since the hash is part of the token signing key.
            updates["password"] = hash_password(updates["password"])
        store.set_settings(updates)
        return await api_get_settings()

    @app.get("/api/health")
    async def api_health():
        config = effective_config()
        problem = inbox_problem(config.inbox, config.min_free_mb)
        return {
            "status": "ok" if not problem else "degraded",
            "inbox": str(config.inbox),
            "inbox_writable": not problem,
            "inbox_problem": problem,
            "inbox_free_mb": inbox_free_mb(config.inbox),
            "min_free_mb": config.min_free_mb,
            "ytdlp_version": ytdlp_version(),
            "version": __version__,
            "active_jobs": manager.active_count(),
            # A wave of these usually means yt-dlp needs updating.
            "failures_last_hour": store.count_failed_since(3600),
        }

    @app.post("/api/ytdlp/update", dependencies=[protected])
    async def api_ytdlp_update():
        """Update the installed yt-dlp package. The running process keeps
        the already-imported version; a container restart loads the new
        one, which the response says explicitly rather than pretending."""
        loaded = ytdlp_version()

        def upgrade():
            import subprocess
            import sys
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--no-cache-dir",
                 "--upgrade", "yt-dlp"],
                capture_output=True, text=True, timeout=300,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip()[-500:] or "pip failed")
            from importlib.metadata import version
            return version("yt-dlp")

        try:
            installed = await asyncio.to_thread(upgrade)
        except Exception as exc:
            raise HTTPException(status_code=502, detail="update failed: %s" % exc)
        return {
            "loaded_version": loaded,
            "installed_version": installed,
            "restart_needed": installed != loaded,
        }

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
