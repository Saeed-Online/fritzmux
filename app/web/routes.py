import logging
import re
from urllib.parse import urlparse
from pathlib import Path

import httpx
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app import epg_manager, m3u_handler, matching, stream_manager
from app.config import MAX_STREAMS, STREAM_START_TIMEOUT
from app.models import ChannelUpdate, ImportRequest, ServerStatus

logger = logging.getLogger(__name__)

router = APIRouter()

_tpl_dir = Path(__file__).parent / "templates"
_jinja_env = Environment(
    loader=FileSystemLoader(str(_tpl_dir)),
    autoescape=select_autoescape(["html", "xml"]),
)

AVM_LOGO_BASE = "https://download.avm.de/tv/logos/"


def err(message: str, status: int = 400, **extra) -> JSONResponse:
    # FastAPI does not turn "return body, status" tuples into a status code.
    return JSONResponse({"error": message, **extra}, status_code=status)


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    tpl = _jinja_env.get_template("index.html")
    html = tpl.render(
        channels=list(m3u_handler.CHANNELS.values()),
        active_streams=stream_manager.active_stream_count(),
        max_streams=MAX_STREAMS,
        epg_sources=epg_manager.EPG_SOURCES,
    )
    return HTMLResponse(html)


@router.get("/api/status")
async def api_status():
    return ServerStatus(
        active_streams=stream_manager.active_stream_count(),
        max_streams=MAX_STREAMS,
        channels_count=len(m3u_handler.CHANNELS),
        epg_sources=[s["name"] for s in epg_manager.EPG_SOURCES],
        viewers=stream_manager.viewer_count(),
    )


@router.get("/api/channels")
async def api_channels():
    return list(m3u_handler.CHANNELS.values())


@router.get("/api/channels.m3u")
async def api_m3u(request: Request):
    base_url = str(request.base_url).rstrip("/")
    content = m3u_handler.generate_m3u(base_url, epg_url=f"{base_url}/api/epg.xml")
    return Response(
        content=content.encode("utf-8"),
        media_type="audio/x-mpegurl; charset=utf-8",
        headers={"Content-Disposition": 'inline; filename="channels.m3u"'},
    )


@router.get("/api/epg.xml")
async def api_epg():
    xml = epg_manager.generate_xmltv(m3u_handler.CHANNELS.values())
    return Response(
        content=xml,
        media_type="application/xml; charset=utf-8",
        headers={"Content-Disposition": 'inline; filename="fritzmux.xml"'},
    )


@router.get("/api/epg/channels")
async def api_epg_channels():
    return epg_manager.get_epg_channels()


@router.get("/api/epg/refresh")
async def api_epg_refresh():
    await epg_manager.fetch_all()
    icons = epg_manager.get_channel_icons()
    changed = False
    for ch in m3u_handler.CHANNELS.values():
        epg_id = epg_manager.resolve_epg_id(ch.tvg_id, [ch.tvg_name, ch.title])
        if not ch.tvg_logo and epg_id in icons:
            ch.tvg_logo = icons[epg_id]
            changed = True
    if changed:
        m3u_handler.save_channels()
    return {"status": "ok", "events": epg_manager.programme_count()}


@router.post("/api/import/url")
async def api_import_url(req: ImportRequest):
    if not req.url:
        return err("URL is required")
    try:
        channels = await m3u_handler.import_from_url(req.url)
        if not channels:
            return {"status": "ok", "imported": 0, "total": len(m3u_handler.CHANNELS),
                    "warning": "URL enthielt keine gültigen M3U-Einträge. Prüfe die URL oder lade die M3U-Datei manuell hoch."}
        added = m3u_handler.merge_channels(channels, replace=req.replace)
        return {"status": "ok", "imported": added, "total": len(m3u_handler.CHANNELS)}
    except httpx.ConnectError:
        return err("Fritzbox nicht erreichbar. Prüfe die IP-Adresse.", 502)
    except httpx.TimeoutException:
        return err("Zeitüberschreitung – Fritzbox antwortet nicht.", 504)
    except Exception as e:
        logger.exception("Import failed")
        return err(str(e))


@router.post("/api/scan/fritzbox")
async def api_scan_fritzbox(ip: str = Form(...), include_radio: bool = Form(False)):
    base = re.sub(r"^https?://", "", ip.strip()).rstrip("/")
    # The Fritzbox keeps HD and SD channels in separate lists. Vodafone carries
    # most unencrypted private channels (RTL, ProSieben, ...) in SD only, so all
    # lists have to be imported, not just the first one found.
    lists = [("tvhd.m3u", "HD"), ("tvsd.m3u", "SD"), ("tvall.m3u", "")]
    if include_radio:
        lists.append(("radio.m3u", "Radio"))
    candidates = [(f"http://{base}/dvb/m3u/{name}", group) for name, group in lists]
    legacy = [
        (f"http://{base}:49000/m3u", ""),
        (f"http://{base}:49000/m3u.m3u", ""),
        (f"http://{base}/cgi-bin/webcm?getpage=../html/de/internet/tvapp.m3u", ""),
        (f"http://{base}/internet/tvapp.m3u", ""),
    ]
    found_urls, found_channels = [], []
    async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
        for group_set in (candidates, legacy):
            for url, group in group_set:
                try:
                    text = await m3u_handler.fetch_playlist(client, url)
                except Exception:
                    continue
                channels = m3u_handler.parse_m3u(text, default_group=group)
                if channels:
                    found_urls.append(url)
                    found_channels.extend(channels)
            if found_channels:
                break
    if not found_channels:
        return err("Keine M3U-Senderliste auf der Fritzbox gefunden. Sendersuchlauf unter DVB-C gemacht? "
                   "Sonst die M3U-Datei manuell hochladen.", 404, status="not_found")
    added = m3u_handler.merge_channels(found_channels, replace=False)
    return {"status": "ok", "url": found_urls[0], "urls": found_urls, "found": len(found_channels),
            "imported": added, "total": len(m3u_handler.CHANNELS)}


@router.post("/api/import/upload")
async def api_import_upload(file: UploadFile = File(...), replace: bool = Form(False)):
    try:
        text = m3u_handler.decode_playlist(await file.read())
        channels = m3u_handler.import_from_text(text)
        if not channels:
            return err("Datei enthält keine gültigen M3U-Einträge.")
        added = m3u_handler.merge_channels(channels, replace=replace)
        return {"status": "ok", "imported": added, "total": len(m3u_handler.CHANNELS)}
    except Exception as e:
        logger.exception("Upload failed")
        return err(str(e))


@router.post("/api/epg/source")
async def api_epg_add_source(request: Request, name: str = Form(...), url: str = Form(...)):
    parsed = urlparse(url.strip())
    if parsed.path.rstrip("/").endswith("/api/epg.xml") and parsed.port in (None, request.url.port):
        return err("Das ist die eigene EPG-Ausgabe von FritzMux, keine Quelle. "
                   "Hier gehört eine externe XMLTV-Datei hin, z.B. "
                   "https://epgshare01.online/epgshare01/epg_ripper_DE1.xml.gz")
    try:
        epg_manager.add_source(name, url)
    except epg_manager.SourceError as e:
        return err(str(e))
    return {"status": "ok"}


@router.get("/api/epg/sources")
async def api_epg_list_sources():
    return epg_manager.EPG_SOURCES


@router.delete("/api/epg/source/{name}")
async def api_epg_remove_source(name: str):
    epg_manager.remove_source(name)
    return {"status": "ok"}


@router.get("/api/channels/{channel_id}")
async def api_channel_detail(channel_id: str):
    ch = m3u_handler.CHANNELS.get(channel_id)
    if not ch:
        return err("not found", 404)
    return ch


@router.delete("/api/channels/{channel_id}")
async def api_channel_delete(channel_id: str):
    m3u_handler.delete_channel(channel_id)
    return {"status": "ok"}


@router.put("/api/channels/{channel_id}")
async def api_channel_update(channel_id: str, update: ChannelUpdate):
    data = update.model_dump(exclude_none=True)
    if "rtsp_url" in data and not data["rtsp_url"].startswith(m3u_handler.ALLOWED_STREAM_SCHEMES):
        return err("Stream-URL muss mit rtsp://, http:// oder https:// beginnen")
    if data.get("tvg_logo") and data["tvg_logo"] != "__uploaded__" \
            and not data["tvg_logo"].startswith(("http://", "https://")):
        return err("Logo-URL muss mit http:// oder https:// beginnen")
    ch = m3u_handler.update_channel(channel_id, data)
    if not ch:
        return err("not found", 404)
    return ch


@router.post("/api/channels/clear")
async def api_channels_clear():
    m3u_handler.clear_channels()
    return {"status": "ok"}


@router.get("/api/logo/{channel_id}")
async def api_logo(channel_id: str):
    ch = m3u_handler.CHANNELS.get(channel_id)
    if not ch or not ch.tvg_logo:
        return Response(status_code=404, content="No logo")

    cache_file, meta_file = m3u_handler.logo_paths(channel_id)
    headers = {"Cache-Control": "public, max-age=86400"}
    if cache_file.exists() and meta_file.exists():
        media_type = meta_file.read_text().strip()
        return Response(content=cache_file.read_bytes(), media_type=media_type, headers=headers)
    if ch.tvg_logo == "__uploaded__":
        return Response(status_code=404, content="Uploaded logo not found")

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(ch.tvg_logo)
            resp.raise_for_status()
        media_type = resp.headers.get("content-type", "image/png").split(";")[0]
        if not media_type.startswith("image/"):
            return Response(status_code=502, content="Logo URL did not return an image")
        cache_file.write_bytes(resp.content)
        meta_file.write_text(media_type)
        return Response(content=resp.content, media_type=media_type, headers=headers)
    except Exception as e:
        logger.warning("Failed to fetch logo for %s: %s", channel_id, e)
        return Response(status_code=502, content="Logo fetch failed")


@router.post("/api/logo/{channel_id}/upload")
async def api_logo_upload(channel_id: str, file: UploadFile = File(...)):
    ch = m3u_handler.CHANNELS.get(channel_id)
    if not ch:
        return err("not found", 404)
    media_type = file.content_type or "image/png"
    if not media_type.startswith("image/"):
        return err("Nur Bilddateien")
    cache_file, meta_file = m3u_handler.logo_paths(channel_id)
    cache_file.write_bytes(await file.read())
    meta_file.write_text(media_type)
    ch.tvg_logo = "__uploaded__"
    m3u_handler.save_channels()
    return {"status": "ok", "logo": f"/api/logo/{channel_id}"}


@router.post("/api/logos/avm")
async def api_fetch_avm_logos():
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(AVM_LOGO_BASE)
            resp.raise_for_status()
            avm_logos = set(re.findall(r'href="([^"/]+\.png)"', resp.text))
    except Exception as e:
        return err(f"AVM-Repository nicht erreichbar: {e}", 502)

    if not avm_logos:
        return err("Keine Logos im AVM-Repository gefunden", 404)

    # compact name -> {"sd": file, "hd": file}
    variants: dict[str, dict[str, str]] = {}
    for fn in avm_logos:
        base = fn[:-4]
        kind = "hd" if base.lower().endswith("_hd") else "sd"
        variants.setdefault(matching.compact(base), {})[kind] = fn
    index = {k: k for k in variants}

    found = 0
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        for ch in m3u_handler.CHANNELS.values():
            cache_file, meta_file = m3u_handler.logo_paths(ch.id)
            if ch.tvg_logo == "__uploaded__" and cache_file.exists():
                found += 1  # uploaded or fetched earlier
                continue
            names = [ch.tvg_name, ch.title]
            key = matching.best_match(names, index, matching.LOGO_ALIASES)
            if not key:
                continue
            v = variants[key]
            fn = (v.get("hd") or v.get("sd")) if matching.wants_hd(names) else (v.get("sd") or v.get("hd"))
            try:
                resp = await client.get(AVM_LOGO_BASE + fn)
                resp.raise_for_status()
            except Exception:
                continue
            cache_file.write_bytes(resp.content)
            meta_file.write_text(resp.headers.get("content-type", "image/png").split(";")[0])
            ch.tvg_logo = "__uploaded__"
            found += 1

    m3u_handler.save_channels()
    return {"status": "ok", "found": found, "total": len(m3u_handler.CHANNELS)}


@router.head("/stream/{channel_id}")
async def stream_channel_head(channel_id: str):
    # Some players probe with HEAD first; answer without occupying a tuner.
    if channel_id not in m3u_handler.CHANNELS:
        return Response(status_code=404)
    return Response(status_code=200, media_type="video/mp2t")


@router.get("/stream/{channel_id}")
async def stream_channel(channel_id: str):
    ch = m3u_handler.CHANNELS.get(channel_id)
    if not ch:
        return Response(status_code=404, content="Channel not found")

    try:
        stream, queue = await stream_manager.subscribe(channel_id, ch.rtsp_url)
    except stream_manager.NoTunerFree:
        return Response(status_code=503, content="All tuners busy")

    first = await stream_manager.first_chunk(queue, STREAM_START_TIMEOUT)
    if not first:
        stream_manager.unsubscribe(stream, queue)
        detail = stream.last_error or f"no data after {STREAM_START_TIMEOUT}s"
        logger.warning("Channel %s failed to start: %s", channel_id, detail)
        return Response(status_code=502, content=f"Stream failed: {detail}")

    async def gen():
        try:
            yield first
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                yield chunk
        finally:
            stream_manager.unsubscribe(stream, queue)

    return StreamingResponse(
        gen(),
        media_type="video/mp2t",
        headers={"Cache-Control": "no-cache"},
    )


@router.post("/api/stream/test")
async def api_stream_test(channel_id: str = Form(...)):
    """Runs the real relay path once and reports whether data arrives."""
    ch = m3u_handler.CHANNELS.get(channel_id)
    if not ch:
        return err("Channel not found", 404)
    try:
        stream, queue = await stream_manager.subscribe(channel_id, ch.rtsp_url)
    except stream_manager.NoTunerFree:
        return {"status": "error", "message": "Alle Tuner belegt"}
    try:
        first = await stream_manager.first_chunk(queue, STREAM_START_TIMEOUT)
    finally:
        stream_manager.unsubscribe(stream, queue)
    if first:
        return {"status": "ok", "message": "ffmpeg liefert Daten"}
    return {"status": "error", "message": stream.last_error or "Keine Daten (Timeout)"}
