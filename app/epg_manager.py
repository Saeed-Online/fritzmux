import asyncio
import gzip
import hashlib
import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Optional
from xml.sax.saxutils import escape, quoteattr

import httpx

from app.config import EPG_CACHE_DIR, EPG_KEEP_PAST_HOURS, EPG_SOURCES_FILE
from app.m3u_handler import _atomic_write
from app.matching import EPG_ALIASES, best_match, compact

logger = logging.getLogger(__name__)

SOURCE_NAME_RE = re.compile(r"^[\w .-]{1,64}$")

EPG_SOURCES: list[dict] = []

# epg channel id -> display name / icon
_epg_channels: dict[str, str] = {}
_channel_icons: dict[str, str] = {}
# (epg channel id, start attr, stop attr, stop as UTC datetime or None, serialized children)
_programmes: list[tuple[str, str, str, Optional[datetime], bytes]] = []
_version = 0
_last_fetch: Optional[datetime] = None
_fetch_lock = asyncio.Lock()
_xml_cache: dict[str, bytes] = {}


class SourceError(ValueError):
    pass


# ---------------------------------------------------------------- sources

def _cache_file(name: str):
    slug = hashlib.sha1(name.encode("utf-8")).hexdigest()[:12]
    return EPG_CACHE_DIR / f"{slug}.xml"


def add_source(name: str, url: str):
    name = name.strip()
    url = url.strip()
    if not SOURCE_NAME_RE.match(name):
        raise SourceError("Ungültiger Name (erlaubt: Buchstaben, Zahlen, Leerzeichen, . _ -)")
    if not url.startswith(("http://", "https://")):
        raise SourceError("Nur http:// oder https:// URLs")
    if any(s["name"] == name for s in EPG_SOURCES):
        raise SourceError("Eine Quelle mit diesem Namen existiert bereits")
    EPG_SOURCES.append({"name": name, "url": url, "enabled": True})
    save_sources()


def remove_source(name: str):
    EPG_SOURCES[:] = [s for s in EPG_SOURCES if s["name"] != name]
    _cache_file(name).unlink(missing_ok=True)
    save_sources()


def save_sources():
    _atomic_write(EPG_SOURCES_FILE, json.dumps(EPG_SOURCES, indent=2, ensure_ascii=False))


def load_sources():
    if EPG_SOURCES_FILE.exists():
        data = json.loads(EPG_SOURCES_FILE.read_text(encoding="utf-8"))
        EPG_SOURCES.clear()
        EPG_SOURCES.extend(data)


# ---------------------------------------------------------------- fetching / parsing

def _maybe_gunzip(data: bytes) -> bytes:
    # httpx already undoes Content-Encoding; .xml.gz files still arrive compressed.
    if data[:2] == b"\x1f\x8b":
        return gzip.decompress(data)
    return data


def _parse_time(value: str) -> Optional[datetime]:
    value = value.strip()
    for fmt in ("%Y%m%d%H%M%S %z", "%Y%m%d%H%M%S"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _parse_file(path) -> tuple[dict, dict, list]:
    """Runs in a worker thread; streaming parse keeps memory low on a Pi."""
    channels: dict[str, str] = {}
    icons: dict[str, str] = {}
    programmes = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=EPG_KEEP_PAST_HOURS)
    try:
        for _, el in ET.iterparse(str(path), events=("end",)):
            if el.tag == "channel":
                ch_id = el.get("id", "")
                dn = el.find("display-name")
                channels[ch_id] = (dn.text or ch_id) if dn is not None else ch_id
                icon = el.find("icon")
                if icon is not None and icon.get("src"):
                    icons[ch_id] = icon.get("src")
                el.clear()
            elif el.tag == "programme":
                stop_raw = el.get("stop", "")
                stop = _parse_time(stop_raw)
                if stop is None or stop >= cutoff:
                    body = "".join(ET.tostring(child, encoding="unicode") for child in el).encode("utf-8")
                    programmes.append((el.get("channel", ""), el.get("start", ""), stop_raw, stop, body))
                el.clear()
    except ET.ParseError as e:
        logger.error("XMLTV parse error in %s: %s", path, e)
    return channels, icons, programmes


async def _rebuild_from_cache():
    global _epg_channels, _channel_icons, _programmes, _version
    channels, icons, programmes = {}, {}, []
    for src in EPG_SOURCES:
        if not src.get("enabled", True):
            continue
        f = _cache_file(src["name"])
        if not f.exists():
            continue
        c, i, p = await asyncio.to_thread(_parse_file, f)
        channels.update(c)
        icons.update(i)
        programmes.extend(p)
        logger.info("EPG %s: %d channels, %d programmes", src["name"], len(c), len(p))
    _epg_channels, _channel_icons, _programmes = channels, icons, programmes
    _version += 1
    _xml_cache.clear()


async def load_cache():
    """Startup: use what is on disk, no network needed."""
    async with _fetch_lock:
        await _rebuild_from_cache()


async def fetch_all():
    global _last_fetch
    async with _fetch_lock:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            for src in EPG_SOURCES:
                if not src.get("enabled", True):
                    continue
                try:
                    resp = await client.get(src["url"])
                    resp.raise_for_status()
                    data = await asyncio.to_thread(_maybe_gunzip, resp.content)
                    target = _cache_file(src["name"])
                    tmp = target.with_suffix(".tmp")
                    await asyncio.to_thread(tmp.write_bytes, data)
                    tmp.replace(target)
                except Exception as e:
                    logger.warning("Failed to fetch EPG from %s: %s (using cache if present)", src["name"], e)
        await _rebuild_from_cache()
        _last_fetch = datetime.now()


# ---------------------------------------------------------------- queries

def get_channel_icons() -> dict[str, str]:
    return dict(_channel_icons)


def get_epg_channels() -> list[dict]:
    return [{"id": k, "name": v} for k, v in _epg_channels.items()]


def programme_count() -> int:
    return len(_programmes)


def _name_index() -> dict[str, str]:
    by_name: dict[str, str] = {}
    for ch_id, dn in _epg_channels.items():
        by_name.setdefault(compact(ch_id), ch_id)
        by_name.setdefault(compact(dn), ch_id)
    by_name.pop("", None)
    return by_name


def resolve_epg_id(tvg_id: str, names: list[str], by_name: Optional[dict] = None) -> Optional[str]:
    """Manual mapping (tvg-id is an EPG id) wins, otherwise match by channel name."""
    if tvg_id in _epg_channels:
        return tvg_id
    if by_name is None:
        by_name = _name_index()
    return best_match(names, by_name, EPG_ALIASES)


def generate_xmltv(channels) -> bytes:
    """channels: iterable of models.Channel. Output uses the playlist's tvg-ids,
    so players match EPG and channels without manual mapping."""
    channels = list(channels)
    now_bucket = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    sig = hashlib.sha1(json.dumps(
        [_version, now_bucket] + [[c.tvg_id, c.tvg_name, c.title] for c in channels]
    ).encode()).hexdigest()
    if sig in _xml_cache:
        return _xml_cache[sig]

    cutoff = datetime.now(timezone.utc) - timedelta(hours=EPG_KEEP_PAST_HOURS)
    targets: dict[str, list[str]] = {}          # epg id -> output channel ids
    out = [b'<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="FritzMux">\n']
    seen_out = set()
    by_name = _name_index()
    for ch in channels:
        epg_id = resolve_epg_id(ch.tvg_id, [ch.tvg_name, ch.title], by_name)
        if not epg_id or ch.tvg_id in seen_out:
            continue
        seen_out.add(ch.tvg_id)
        targets.setdefault(epg_id, []).append(ch.tvg_id)
        out.append(f"  <channel id={quoteattr(ch.tvg_id)}>\n"
                   f"    <display-name>{escape(ch.tvg_name or ch.title)}</display-name>\n"
                   f"  </channel>\n".encode("utf-8"))

    for epg_id, start, stop_raw, stop, body in _programmes:
        outs = targets.get(epg_id)
        if not outs or (stop is not None and stop < cutoff):
            continue
        for out_id in outs:
            out.append(f"  <programme start={quoteattr(start)} stop={quoteattr(stop_raw)} "
                       f"channel={quoteattr(out_id)}>".encode("utf-8"))
            out.append(body)
            out.append(b"</programme>\n")
    out.append(b"</tv>\n")
    result = b"".join(out)
    _xml_cache.clear()
    _xml_cache[sig] = result
    return result


def listings(tvg_id: str, names: list[str], limit: int = 4) -> list[dict]:
    """Current and upcoming programmes for one channel (for the Xtream API)."""
    epg_id = resolve_epg_id(tvg_id, names)
    if not epg_id:
        return []
    now = datetime.now(timezone.utc)
    items = []
    for ch_id, start_raw, stop_raw, stop, body in _programmes:
        if ch_id != epg_id or stop is None or stop < now:
            continue
        start = _parse_time(start_raw)
        if start is None:
            continue
        try:
            el = ET.fromstring(b"<p>" + body + b"</p>")
        except ET.ParseError:
            continue
        items.append({
            "start": start,
            "stop": stop,
            "title": (el.findtext("title") or "").strip(),
            "desc": (el.findtext("desc") or "").strip(),
        })
    items.sort(key=lambda x: x["start"])
    return items[:limit]
