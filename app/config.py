import os
from pathlib import Path


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


DATA_DIR = Path(os.environ.get("FRITZMUX_DATA_DIR", "/app/data"))
CHANNELS_FILE = DATA_DIR / "channels.json"
EPG_CACHE_DIR = DATA_DIR / "epg"
EPG_SOURCES_FILE = DATA_DIR / "epg_sources.json"
LOGO_DIR = DATA_DIR / "logos"

DEFAULT_FFMPEG_PATH = os.environ.get("FRITZMUX_FFMPEG", "ffmpeg")
# RTSP transport towards the Fritzbox. The Fritzbox answers TCP with
# "461 Unsupported Transport", so UDP is the default.
RTSP_TRANSPORT = os.environ.get("FRITZMUX_RTSP_TRANSPORT", "udp")

# The Fritzbox Cable models have a quad tuner.
MAX_STREAMS = _int_env("FRITZMUX_MAX_STREAMS", 4)
# Seconds a stream keeps running after its last viewer left
# (makes player reconnects and "back to the previous channel" instant).
# An idle stream is stopped immediately if its tuner is needed for another channel.
STREAM_TIMEOUT = _int_env("FRITZMUX_STREAM_TIMEOUT", 15)
# Tracks sent to players: "main" = first video + first audio (works on TVs),
# "all" = every audio track + teletext/subtitles (VLC, Kodi, TiviMate).
STREAM_TRACKS = os.environ.get("FRITZMUX_TRACKS", "main").strip().lower()
# Per-viewer buffer before a viewer that cannot keep up is disconnected
# (64 MB is roughly 40-60 s of an HD channel).
VIEWER_BUFFER_BYTES = _int_env("FRITZMUX_VIEWER_BUFFER_MB", 64) * 1024 * 1024
# Seconds to wait for the first video data from ffmpeg.
STREAM_START_TIMEOUT = _int_env("FRITZMUX_STREAM_START_TIMEOUT", 10)

EPG_FETCH_INTERVAL = _int_env("FRITZMUX_EPG_INTERVAL", 3600)
# Programmes that ended longer ago than this are not served.
EPG_KEEP_PAST_HOURS = _int_env("FRITZMUX_EPG_KEEP_PAST_HOURS", 6)

# Optional HTTP Basic auth for the web UI and the admin API.
# Playlist, EPG, logos and streams stay open so IPTV players keep working.
AUTH_USER = os.environ.get("FRITZMUX_USER", "")
AUTH_PASSWORD = os.environ.get("FRITZMUX_PASSWORD", "")

# Xtream Codes login (IPTV Smarters etc.). Empty = any username/password is accepted
# (defaults to FRITZMUX_USER/FRITZMUX_PASSWORD when those are set).
XTREAM_USER = os.environ.get("FRITZMUX_XTREAM_USER", AUTH_USER)
XTREAM_PASSWORD = os.environ.get("FRITZMUX_XTREAM_PASSWORD", AUTH_PASSWORD)

DATA_DIR.mkdir(parents=True, exist_ok=True)
EPG_CACHE_DIR.mkdir(parents=True, exist_ok=True)
LOGO_DIR.mkdir(parents=True, exist_ok=True)
