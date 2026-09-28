"""On-demand RTSP -> HTTP relay.

One ffmpeg process per channel, shared by all viewers of that channel
(fan-out through one queue per viewer). ffmpeg's stderr is drained
continuously so the process can never block on a full pipe.
"""
import asyncio
import re
import logging
import signal
import time
from typing import Optional

from app.config import (
    DEFAULT_FFMPEG_PATH,
    MAX_STREAMS,
    RTSP_TRANSPORT,
    STREAM_TRACKS,
    STREAM_TIMEOUT,
    VIEWER_BUFFER_BYTES,
)

logger = logging.getLogger(__name__)

TS_PACKET = 188
READ_SIZE = TS_PACKET * 348          # ~64 KiB, always whole TS packets
STDERR_LOG_BURST = 20                # max ffmpeg log lines per 10 s window


_NOISE = re.compile(r"non-existing PPS|decode_slice_header error|no frame!|Last message repeated")


class NoTunerFree(Exception):
    """All tuners are busy with channels that have viewers."""


class Viewer:
    """Per-viewer buffer, limited by bytes (ffmpeg writes many small chunks,
    so a chunk-count limit was only ~1-5 s of HD video)."""

    def __init__(self):
        self._q: asyncio.Queue = asyncio.Queue()
        self.backlog = 0

    def put(self, chunk: Optional[bytes]) -> bool:
        if chunk is not None:
            if self.backlog + len(chunk) > VIEWER_BUFFER_BYTES:
                return False
            self.backlog += len(chunk)
        self._q.put_nowait(chunk)
        return True

    async def get(self) -> Optional[bytes]:
        chunk = await self._q.get()
        if chunk is not None:
            self.backlog -= len(chunk)
        return chunk


class _Stream:
    def __init__(self, channel_id: str, url: str):
        self.channel_id = channel_id
        self.url = url
        self.process: Optional[asyncio.subprocess.Process] = None
        self.subscribers: set[Viewer] = set()
        self.idle_since: Optional[float] = None
        self.stop_timer: Optional[asyncio.TimerHandle] = None
        self.last_error = ""
        self.finished = False
        self.tasks: list[asyncio.Task] = []

    @property
    def alive(self) -> bool:
        return (
            not self.finished
            and self.process is not None
            and self.process.returncode is None
        )


_streams: dict[str, _Stream] = {}
_lock = asyncio.Lock()


def _track_maps() -> list[str]:
    if STREAM_TRACKS == "all":
        # every audio track (Dolby, audio description, ...) plus teletext/subtitles:
        # fine for VLC/Kodi/TiviMate, but TV players (LG webOS) stall after ~10 s
        # because they buffer the unused tracks until their queue is full
        return ["-map", "0:v?", "-map", "0:a?", "-map", "0:s?"]
    # default: first video + first audio (main German stereo track on DVB-C)
    return ["-map", "0:v:0?", "-map", "0:a:0?"]


def _ffmpeg_args(url: str) -> list[str]:
    args = [DEFAULT_FFMPEG_PATH, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "error"]
    if url.startswith("rtsp://"):
        args += [
            "-rtsp_transport", RTSP_TRANSPORT,
            "-timeout", "5000000",
            "-protocol_whitelist", "rtsp,rtp,udp,tcp",
        ]
    else:
        args += ["-protocol_whitelist", "http,https,tls,tcp,udp,rtp,crypto"]
    args += [
        "-i", url,
        *_track_maps(),
        "-c", "copy",
        "-f", "mpegts",
        "pipe:1",
    ]
    return args


def _push_eof(q: Viewer):
    q.put(None)


async def _drain_stderr(s: _Stream):
    proc = s.process
    assert proc is not None and proc.stderr is not None
    window_start = time.monotonic()
    logged = suppressed = 0
    pending = b""
    while True:
        data = await proc.stderr.read(4096)
        if not data:
            break
        # ffmpeg ends progress lines with \r; cap the tail so it can never grow unbounded.
        pending = (pending + data).replace(b"\r", b"\n")
        *lines, pending = pending.split(b"\n")
        pending = pending[-4096:]
        for raw in lines:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            if _NOISE.search(line):
                continue  # harmless decoder messages while ffmpeg probes the first frames
            s.last_error = line[-300:]
            now = time.monotonic()
            if now - window_start > 10:
                if suppressed:
                    logger.warning("ffmpeg[%s]: %d more lines suppressed", s.channel_id, suppressed)
                window_start, logged, suppressed = now, 0, 0
            if logged < STDERR_LOG_BURST:
                logger.warning("ffmpeg[%s]: %s", s.channel_id, line)
                logged += 1
            else:
                suppressed += 1


async def _pump(s: _Stream):
    proc = s.process
    assert proc is not None and proc.stdout is not None
    rest = b""
    try:
        while True:
            data = await proc.stdout.read(READ_SIZE)
            if not data:
                break
            data = rest + data
            cut = len(data) - len(data) % TS_PACKET
            chunk, rest = data[:cut], data[cut:]
            if not chunk:
                continue
            for q in list(s.subscribers):
                if not q.put(chunk):
                    logger.warning("Viewer of channel %s stopped reading (%d MB behind: player "
                                   "stalled, idle probe connection or slow network), disconnecting it",
                                   s.channel_id, q.backlog // 1_000_000)
                    s.subscribers.discard(q)
                    _push_eof(q)
                    if not s.subscribers:
                        unsubscribe(s, q)
    except Exception:
        logger.exception("Relay error for channel %s", s.channel_id)
    finally:
        s.finished = True
        if proc.returncode is None:
            proc.terminate()
        try:
            rc = await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            rc = await proc.wait()
        logger.info("ffmpeg for channel %s ended (rc=%s)", s.channel_id, rc)
        if s.stop_timer:
            s.stop_timer.cancel()
        for q in list(s.subscribers):
            _push_eof(q)
        s.subscribers.clear()
        if _streams.get(s.channel_id) is s:
            del _streams[s.channel_id]


async def _start(channel_id: str, url: str) -> _Stream:
    s = _Stream(channel_id, url)
    logger.info("Starting ffmpeg for channel %s: %s", channel_id, url)
    s.process = await asyncio.create_subprocess_exec(
        *_ffmpeg_args(url),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        preexec_fn=lambda: signal.signal(signal.SIGPIPE, signal.SIG_DFL),
    )
    s.tasks = [asyncio.create_task(_drain_stderr(s)), asyncio.create_task(_pump(s))]
    _streams[channel_id] = s
    return s


async def _terminate(s: _Stream):
    if s.process and s.process.returncode is None:
        logger.info("Stopping ffmpeg for channel %s", s.channel_id)
        s.process.terminate()
    # _pump() reaps the process and removes the stream.
    await asyncio.gather(*s.tasks, return_exceptions=True)


async def subscribe(channel_id: str, url: str) -> tuple[_Stream, Viewer]:
    async with _lock:
        s = _streams.get(channel_id)
        if s and (not s.alive or s.url != url):
            await _terminate(s)
            s = None
        if s is None:
            running = [x for x in _streams.values() if x.alive]
            if len(running) >= MAX_STREAMS:
                idle = [x for x in running if not x.subscribers]
                if not idle:
                    raise NoTunerFree()
                # Free the tuner that has been idle the longest (channel zapping).
                await _terminate(min(idle, key=lambda x: x.idle_since or 0))
            s = await _start(channel_id, url)
        q = Viewer()
        s.subscribers.add(q)
        s.idle_since = None
        if s.stop_timer:
            s.stop_timer.cancel()
            s.stop_timer = None
        return s, q


def unsubscribe(s: _Stream, q: Viewer):
    """Synchronous on purpose: safe to call from a cancelled generator's finally."""
    s.subscribers.discard(q)
    if s.subscribers or not s.alive:
        return
    s.idle_since = time.monotonic()
    loop = asyncio.get_running_loop()

    def _expire():
        if not s.subscribers and s.alive:
            loop.create_task(_terminate(s))

    if s.stop_timer:
        s.stop_timer.cancel()
    s.stop_timer = loop.call_later(STREAM_TIMEOUT, _expire)


async def first_chunk(q: Viewer, timeout: float) -> Optional[bytes]:
    try:
        return await asyncio.wait_for(q.get(), timeout=timeout)
    except asyncio.TimeoutError:
        return None


def active_stream_count() -> int:
    return sum(1 for s in _streams.values() if s.alive)


def viewer_count() -> int:
    return sum(len(s.subscribers) for s in _streams.values() if s.alive)


async def stop_all():
    await asyncio.gather(*(_terminate(s) for s in list(_streams.values())), return_exceptions=True)
