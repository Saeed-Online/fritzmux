"""HLS output for players that cannot play a raw MPEG-TS HTTP stream
(AVPlayer on iPhone/Apple TV, many phone IPTV apps) or cannot decode
the MP2 audio used by German DVB-C channels.

Each HLS session is just another viewer of the shared channel stream
(no extra tuner): a second ffmpeg reads the TS from that viewer, copies
the video and converts the first audio track to AAC stereo.
"""
import asyncio
import logging
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional

from app import stream_manager
from app.config import DEFAULT_FFMPEG_PATH, STREAM_START_TIMEOUT

logger = logging.getLogger(__name__)

HLS_ROOT = Path(tempfile.gettempdir()) / "fritzmux-hls"
SEGMENT_SECONDS = 2
PLAYLIST_SIZE = 8
IDLE_TIMEOUT = 30          # stop when no playlist/segment request for this long
READY_SEGMENTS = 2         # segments needed before the playlist is served


class _Session:
    def __init__(self, channel_id: str):
        self.channel_id = channel_id
        self.dir = HLS_ROOT / channel_id
        self.process: Optional[asyncio.subprocess.Process] = None
        self.stream = None
        self.viewer = None
        self.tasks: list[asyncio.Task] = []
        self.last_access = time.monotonic()
        self.last_error = ""

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    def ready(self) -> bool:
        playlist = self.dir / "index.m3u8"
        if not playlist.exists():
            return False
        return playlist.read_text(errors="replace").count("#EXTINF") >= READY_SEGMENTS


_sessions: dict[str, _Session] = {}
_lock = asyncio.Lock()
_reaper: Optional[asyncio.Task] = None


async def _feed(s: _Session):
    """Copy TS chunks from the shared channel stream into ffmpeg's stdin."""
    assert s.process and s.process.stdin
    try:
        while True:
            chunk = await s.viewer.get()
            if chunk is None:
                break
            s.process.stdin.write(chunk)
            await s.process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        try:
            s.process.stdin.close()
        except Exception:
            pass


async def _drain_stderr(s: _Session):
    assert s.process and s.process.stderr
    while True:
        line = await s.process.stderr.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="replace").strip()
        if text:
            s.last_error = text[-300:]
            logger.warning("hls[%s]: %s", s.channel_id, text)


async def _start(channel_id: str, url: str) -> _Session:
    s = _Session(channel_id)
    shutil.rmtree(s.dir, ignore_errors=True)
    s.dir.mkdir(parents=True, exist_ok=True)
    s.stream, s.viewer = await stream_manager.subscribe(channel_id, url)
    args = [
        DEFAULT_FFMPEG_PATH, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "error",
        "-fflags", "+genpts", "-f", "mpegts", "-i", "pipe:0",
        "-map", "0:v:0?", "-map", "0:a:0?",
        "-c:v", "copy",
        "-c:a", "aac", "-ac", "2", "-b:a", "160k",
        "-f", "hls",
        "-hls_time", str(SEGMENT_SECONDS),
        "-hls_list_size", str(PLAYLIST_SIZE),
        "-hls_flags", "delete_segments+independent_segments+omit_endlist",
        "-hls_segment_filename", str(s.dir / "seg_%06d.ts"),
        str(s.dir / "index.m3u8"),
    ]
    logger.info("Starting HLS for channel %s", channel_id)
    s.process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    s.tasks = [asyncio.create_task(_feed(s)), asyncio.create_task(_drain_stderr(s))]
    _sessions[channel_id] = s
    return s


async def _stop(s: _Session):
    logger.info("Stopping HLS for channel %s", s.channel_id)
    if _sessions.get(s.channel_id) is s:
        del _sessions[s.channel_id]
    if s.viewer is not None:
        stream_manager.unsubscribe(s.stream, s.viewer)
        s.viewer.put(None)
    if s.process and s.process.returncode is None:
        s.process.terminate()
        try:
            await asyncio.wait_for(s.process.wait(), timeout=5)
        except asyncio.TimeoutError:
            s.process.kill()
    for t in s.tasks:
        t.cancel()
    shutil.rmtree(s.dir, ignore_errors=True)


async def _reap_loop():
    while True:
        await asyncio.sleep(5)
        now = time.monotonic()
        for s in list(_sessions.values()):
            if not s.alive or now - s.last_access > IDLE_TIMEOUT:
                await _stop(s)


def _ensure_reaper():
    global _reaper
    if _reaper is None or _reaper.done():
        _reaper = asyncio.create_task(_reap_loop())


async def get_playlist(channel_id: str, url: str) -> tuple[Optional[str], str]:
    """Returns (playlist text, error)."""
    _ensure_reaper()
    async with _lock:
        s = _sessions.get(channel_id)
        if s and not s.alive:
            await _stop(s)
            s = None
        if s is None:
            s = await _start(channel_id, url)
    s.last_access = time.monotonic()
    deadline = time.monotonic() + STREAM_START_TIMEOUT + READY_SEGMENTS * SEGMENT_SECONDS + 5
    while not s.ready():
        if not s.alive or time.monotonic() > deadline:
            error = s.last_error or s.stream.last_error or "HLS not ready"
            await _stop(s)
            return None, error
        await asyncio.sleep(0.25)
    s.last_access = time.monotonic()
    return (s.dir / "index.m3u8").read_text(), ""


def segment_path(channel_id: str, name: str) -> Optional[Path]:
    s = _sessions.get(channel_id)
    if not s or "/" in name or not name.startswith("seg_") or not name.endswith(".ts"):
        return None
    s.last_access = time.monotonic()
    p = s.dir / name
    return p if p.exists() else None


async def stop_all():
    for s in list(_sessions.values()):
        await _stop(s)
