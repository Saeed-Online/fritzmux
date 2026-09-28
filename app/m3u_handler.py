import json
import os
import re
import tempfile

import httpx

from app.config import CHANNELS_FILE, LOGO_DIR
from app.models import Channel


EXTINF_RE = re.compile(r'#EXTINF:\s*(?P<duration>-?\d+(?:\.\d+)?)(?P<rest>.*)')
ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')

ALLOWED_STREAM_SCHEMES = ("rtsp://", "http://", "https://")

CHANNELS: dict[str, Channel] = {}


def _atomic_write(path, text: str):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def load_channels():
    global CHANNELS
    if CHANNELS_FILE.exists():
        data = json.loads(CHANNELS_FILE.read_text(encoding="utf-8"))
        CHANNELS = {ch["id"]: Channel(**ch) for ch in data}
    else:
        CHANNELS = {}


def save_channels():
    _atomic_write(
        CHANNELS_FILE,
        json.dumps([ch.model_dump() for ch in CHANNELS.values()], indent=2, ensure_ascii=False),
    )


def decode_playlist(raw: bytes) -> str:
    """Fritzbox and other sources are not consistent about UTF-8 vs. Latin-1."""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _split_extinf(line: str) -> tuple[dict, str] | None:
    m = EXTINF_RE.match(line)
    if not m:
        return None
    rest = m.group("rest")
    # The title starts after the first comma that is not inside a quoted attribute value.
    in_quotes = False
    for i, c in enumerate(rest):
        if c == '"':
            in_quotes = not in_quotes
        elif c == "," and not in_quotes:
            attrs = dict(ATTR_RE.findall(rest[:i]))
            return attrs, rest[i + 1:].strip()
    return dict(ATTR_RE.findall(rest)), ""


def _slug(title: str) -> str:
    # Stable, readable tvg-id for playlists without one (the Fritzbox lists have none).
    # A running number would repeat across the HD and SD lists and mix up the EPG.
    t = title.replace("ü", "ue").replace("ö", "oe").replace("ä", "ae").replace("ß", "ss")
    t = t.replace("Ü", "Ue").replace("Ö", "Oe").replace("Ä", "Ae")
    return re.sub(r"[^A-Za-z0-9]+", "_", t).strip("_")


def parse_m3u(content: str, default_group: str = "") -> list[Channel]:
    channels = []
    lines = content.strip().splitlines()
    i = 0
    idx = 1
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTINF"):
            parsed = _split_extinf(line)
            i += 1
            # skip comment lines (e.g. #EXTVLCOPT) between EXTINF and URL
            while i < len(lines) and (not lines[i].strip() or lines[i].strip().startswith("#")):
                if lines[i].strip().startswith("#EXTINF"):
                    break
                i += 1
            if parsed and i < len(lines) and not lines[i].strip().startswith("#"):
                attrs, title = parsed
                url = lines[i].strip()
                title = title or attrs.get("tvg-name") or f"Kanal {idx}"
                channels.append(Channel(
                    id=str(idx),
                    tvg_id=attrs.get("tvg-id") or _slug(title) or str(idx),
                    tvg_name=attrs.get("tvg-name") or title,
                    tvg_logo=attrs.get("tvg-logo") or "",
                    group_title=attrs.get("group-title") or default_group,
                    title=title,
                    rtsp_url=url,
                ))
                idx += 1
                i += 1
            continue
        i += 1
    return channels


async def fetch_playlist(client: httpx.AsyncClient, url: str) -> str:
    resp = await client.get(url)
    resp.raise_for_status()
    return decode_playlist(resp.content)


async def import_from_url(url: str) -> list[Channel]:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        return parse_m3u(await fetch_playlist(client, url))


def import_from_text(content: str) -> list[Channel]:
    return parse_m3u(content)


# ---------------------------------------------------------------- logo cache

def logo_paths(channel_id: str):
    return LOGO_DIR / channel_id, LOGO_DIR / f"{channel_id}.meta"


def drop_logo_cache(channel_id: str):
    for p in logo_paths(channel_id):
        p.unlink(missing_ok=True)


def drop_all_logo_caches():
    for p in LOGO_DIR.iterdir():
        if p.is_file():
            p.unlink(missing_ok=True)


# ---------------------------------------------------------------- mutations

def merge_channels(new_channels: list[Channel], replace: bool = False) -> int:
    global CHANNELS
    if replace:
        CHANNELS = {}
        # IDs restart at 1, cached logos would belong to other channels now.
        drop_all_logo_caches()
    existing_urls = {ch.rtsp_url for ch in CHANNELS.values()}
    used_tvg_ids = {ch.tvg_id for ch in CHANNELS.values()}
    next_id = max((int(c.id) for c in CHANNELS.values()), default=0) + 1
    added = 0
    for ch in new_channels:
        if ch.rtsp_url in existing_urls:
            continue
        ch.id = str(next_id)
        base_tvg, n = ch.tvg_id, 2
        while ch.tvg_id in used_tvg_ids:
            ch.tvg_id = f"{base_tvg}_{n}"
            n += 1
        used_tvg_ids.add(ch.tvg_id)
        drop_logo_cache(ch.id)
        CHANNELS[ch.id] = ch
        existing_urls.add(ch.rtsp_url)
        next_id += 1
        added += 1
    save_channels()
    return added


def update_channel(channel_id: str, updates: dict) -> Channel | None:
    ch = CHANNELS.get(channel_id)
    if not ch:
        return None
    new_logo = updates.get("tvg_logo")
    if new_logo is not None and new_logo != ch.tvg_logo and new_logo != "__uploaded__":
        drop_logo_cache(channel_id)
    for key, val in updates.items():
        if val is not None and hasattr(ch, key):
            setattr(ch, key, val)
    CHANNELS[channel_id] = ch
    save_channels()
    return ch


def delete_channel(channel_id: str) -> bool:
    if channel_id not in CHANNELS:
        return False
    del CHANNELS[channel_id]
    drop_logo_cache(channel_id)
    save_channels()
    return True


def clear_channels():
    CHANNELS.clear()
    drop_all_logo_caches()
    save_channels()


# ---------------------------------------------------------------- output

def _attr(value: str) -> str:
    # M3U has no escaping for quotes inside attribute values.
    return value.replace('"', "'").replace("\n", " ").replace("\r", " ")


def generate_m3u(base_url: str = "http://localhost:8181", epg_url: str = "", hls: bool = False) -> str:
    header = "#EXTM3U"
    if epg_url:
        header += f' url-tvg="{epg_url}" x-tvg-url="{epg_url}"'
    lines = [header]
    for ch in sorted(CHANNELS.values(), key=lambda c: int(c.id)):
        attrs = f'tvg-id="{_attr(ch.tvg_id)}" tvg-name="{_attr(ch.tvg_name)}"'
        if ch.tvg_logo:
            attrs += f' tvg-logo="{base_url}/api/logo/{ch.id}"'
        if ch.group_title:
            attrs += f' group-title="{_attr(ch.group_title)}"'
        title = ch.title.replace("\n", " ").replace("\r", " ")
        lines.append(f"#EXTINF:-1 {attrs},{title}")
        lines.append(f"{base_url}/hls/{ch.id}/index.m3u8" if hls else f"{base_url}/stream/{ch.id}")
    return "\n".join(lines) + "\n"
