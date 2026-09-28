import asyncio
import base64
import re
import logging
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from app import epg_manager, hls_manager, m3u_handler, stream_manager
from app.config import AUTH_PASSWORD, AUTH_USER, EPG_FETCH_INTERVAL
from app.web.routes import router
from app.web.xtream import router as xtream_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def _epg_loop(shutdown: asyncio.Event):
    # First refresh right away (in the background, the server is already serving).
    while not shutdown.is_set():
        if epg_manager.EPG_SOURCES:
            try:
                logger.info("EPG refresh...")
                await epg_manager.fetch_all()
                logger.info("EPG refresh done (%d programmes)", epg_manager.programme_count())
            except Exception:
                logger.exception("EPG refresh failed")
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=EPG_FETCH_INTERVAL)
        except asyncio.TimeoutError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("FritzMux starting up...")
    m3u_handler.load_channels()
    logger.info("Loaded %d channels", len(m3u_handler.CHANNELS))
    epg_manager.load_sources()
    await epg_manager.load_cache()
    logger.info("Loaded %d EPG sources from cache", len(epg_manager.EPG_SOURCES))
    shutdown = asyncio.Event()
    epg_task = asyncio.create_task(_epg_loop(shutdown))
    try:
        yield
    finally:
        shutdown.set()
        epg_task.cancel()
        await hls_manager.stop_all()
        await stream_manager.stop_all()


app = FastAPI(title="FritzMux", version="1.1.0", lifespan=lifespan)

# Paths IPTV players need; they never get credentials.
_PUBLIC_PREFIXES = ("/api/channels.m3u", "/api/epg.xml", "/api/logo/", "/stream/", "/hls/", "/api/status",
                    # Xtream Codes API: checks its own username/password
                    "/player_api.php", "/get.php", "/xmltv.php", "/live/")
_XTREAM_SHORT_STREAM = re.compile(r"^/[^/]+/[^/]+/\d+(\.\w+)?$")


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    path = request.url.path
    public = request.method in ("GET", "HEAD") and (
        path.startswith(_PUBLIC_PREFIXES)
        or (not path.startswith("/api/") and bool(_XTREAM_SHORT_STREAM.match(path)))
    ) or path in ("/player_api.php", "/get.php", "/xmltv.php") or request.method == "OPTIONS"
    if not (AUTH_USER and AUTH_PASSWORD) or public:
        return await call_next(request)
    header = request.headers.get("authorization", "")
    if header.lower().startswith("basic "):
        try:
            user, _, pwd = base64.b64decode(header[6:]).decode("utf-8").partition(":")
            if secrets.compare_digest(user, AUTH_USER) and secrets.compare_digest(pwd, AUTH_PASSWORD):
                return await call_next(request)
        except Exception:
            pass
    return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="FritzMux"'})


# TV apps (LG webOS, Samsung Tizen) are web apps: without CORS headers the
# browser engine hides the response and the app reports "Authorization failed".
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "HEAD", "OPTIONS"],
    allow_headers=["*"],
)
app.include_router(router)
app.include_router(xtream_router)  # last: contains a /{user}/{pass}/{stream} catch-all
