"""Minimal Xtream Codes API so apps that only offer an Xtream login
(IPTV Smarters Pro on LG/Samsung TVs, many others) can use FritzMux.

Login in the app:  server = http://<fritzmux-ip>:8181, any username/password
(or FRITZMUX_XTREAM_USER / FRITZMUX_XTREAM_PASSWORD when set).
"""
import base64
import secrets
import time
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app import epg_manager, m3u_handler
from app.config import MAX_STREAMS, XTREAM_PASSWORD, XTREAM_USER
from app.web import routes

router = APIRouter()

_STARTED = int(time.time())


def _authorized(username: str, password: str) -> bool:
    if not (XTREAM_USER and XTREAM_PASSWORD):
        return bool(username)          # open mode: any login works
    return secrets.compare_digest(username or "", XTREAM_USER) and \
        secrets.compare_digest(password or "", XTREAM_PASSWORD)


def _groups() -> dict[str, str]:
    """group name -> category_id (stable order of first appearance)."""
    ids: dict[str, str] = {}
    for ch in sorted(m3u_handler.CHANNELS.values(), key=lambda c: int(c.id)):
        g = ch.group_title or "TV"
        if g not in ids:
            ids[g] = str(len(ids) + 1)
    return ids


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _login_info(request: Request, username: str, password: str) -> dict:
    now = datetime.now()
    return {
        "user_info": {
            "username": username,
            "password": password,
            "message": "FritzMux",
            "auth": 1,
            "status": "Active",
            "exp_date": "4102444800",   # 2100-01-01: some apps reject null
            "is_trial": "0",
            "active_cons": "0",
            "created_at": str(_STARTED),
            "max_connections": str(MAX_STREAMS),
            "allowed_output_formats": ["ts", "m3u8"],
        },
        "server_info": {
            "url": request.url.hostname,
            "port": str(request.url.port or 80),
            "https_port": "443",
            "server_protocol": request.url.scheme,
            "rtmp_port": "0",
            "timezone": "Europe/Berlin",
            "timestamp_now": int(now.timestamp()),
            "time_now": now.strftime("%Y-%m-%d %H:%M:%S"),
        },
    }


def _live_streams(request: Request, category_id: str = "") -> list[dict]:
    groups = _groups()
    base = _base(request)
    out = []
    for num, ch in enumerate(sorted(m3u_handler.CHANNELS.values(), key=lambda c: int(c.id)), start=1):
        cat = groups[ch.group_title or "TV"]
        if category_id and category_id != cat:
            continue
        out.append({
            "num": num,
            "name": ch.title,
            "stream_type": "live",
            "stream_id": int(ch.id),
            "stream_icon": f"{base}/api/logo/{ch.id}" if ch.tvg_logo else "",
            "epg_channel_id": ch.tvg_id,
            "added": str(_STARTED),
            "is_adult": "0",
            "category_id": cat,
            "category_ids": [int(cat)],
            "custom_sid": "",
            "tv_archive": 0,
            "direct_source": "",
            "tv_archive_duration": 0,
        })
    return out


def _short_epg(stream_id: str, limit: int) -> dict:
    ch = m3u_handler.CHANNELS.get(str(stream_id))
    if not ch:
        return {"epg_listings": []}
    now = int(time.time())
    listings = []
    for i, p in enumerate(epg_manager.listings(ch.tvg_id, [ch.tvg_name, ch.title], limit)):
        start, stop = int(p["start"].timestamp()), int(p["stop"].timestamp())
        listings.append({
            "id": f"{ch.id}{start}",
            "epg_id": ch.tvg_id,
            "title": _b64(p["title"]),
            "lang": "de",
            "start": datetime.fromtimestamp(start).strftime("%Y-%m-%d %H:%M:%S"),
            "end": datetime.fromtimestamp(stop).strftime("%Y-%m-%d %H:%M:%S"),
            "description": _b64(p["desc"]),
            "channel_id": ch.tvg_id,
            "start_timestamp": str(start),
            "stop_timestamp": str(stop),
            "now_playing": 1 if start <= now < stop else 0,
            "has_archive": 0,
        })
    return {"epg_listings": listings}


async def _params(request: Request) -> dict:
    """Xtream clients send the login either in the query string (GET) or as a
    form body (POST, e.g. newer IPTV Smarters versions)."""
    params = dict(request.query_params)
    if request.method == "POST":
        try:
            form = await request.form()
            params.update({k: v for k, v in form.items() if isinstance(v, str)})
        except Exception:
            pass
    return params


@router.api_route("/player_api.php", methods=["GET", "POST"])
async def player_api(request: Request):
    p = await _params(request)
    username, password = p.get("username", ""), p.get("password", "")
    action, category_id, stream_id = p.get("action", ""), p.get("category_id", ""), p.get("stream_id", "")
    try:
        limit = int(p.get("limit", 4))
    except ValueError:
        limit = 4
    if not _authorized(username, password):
        return JSONResponse({"user_info": {"auth": 0}})
    if not action:
        return _login_info(request, username, password)
    if action == "get_live_categories":
        return [{"category_id": cid, "category_name": name, "parent_id": 0}
                for name, cid in _groups().items()]
    if action == "get_live_streams":
        return _live_streams(request, category_id)
    if action in ("get_short_epg", "get_simple_data_table"):
        return _short_epg(stream_id, limit if action == "get_short_epg" else 50)
    if action in ("get_vod_categories", "get_vod_streams", "get_series_categories", "get_series"):
        return []
    return JSONResponse({"error": f"unsupported action {action}"}, status_code=400)


@router.api_route("/get.php", methods=["GET", "POST"])
async def get_playlist(request: Request):
    p = await _params(request)
    username, password, output = p.get("username", ""), p.get("password", ""), p.get("output", "ts")
    if not _authorized(username, password):
        return Response(status_code=401)
    base = _base(request)
    content = m3u_handler.generate_m3u(base, epg_url=f"{base}/api/epg.xml", hls=(output == "m3u8"))
    return Response(content=content.encode("utf-8"), media_type="audio/x-mpegurl; charset=utf-8")


@router.api_route("/xmltv.php", methods=["GET", "POST"])
async def xmltv(request: Request):
    p = await _params(request)
    if not _authorized(p.get("username", ""), p.get("password", "")):
        return Response(status_code=401)
    return await routes.api_epg()


@router.api_route("/live/{username}/{password}/{stream}", methods=["GET", "HEAD"])
@router.api_route("/{username}/{password}/{stream}", methods=["GET", "HEAD"])
async def live(request: Request, username: str, password: str, stream: str):
    if not _authorized(username, password):
        return Response(status_code=401)
    channel_id, _, ext = stream.partition(".")
    if not channel_id.isdigit():
        return Response(status_code=404)
    if request.method == "HEAD":
        return await routes.stream_channel_head(channel_id)
    if ext == "m3u8":
        resp = await routes.hls_playlist(channel_id)
        if resp.status_code != 200:
            return resp
        # segment names are relative to /hls/<id>/, make them absolute
        text = resp.body.decode("utf-8")
        text = "\n".join(f"/hls/{channel_id}/{line}" if line.startswith("seg_") else line
                         for line in text.splitlines()) + "\n"
        return Response(content=text, media_type="application/vnd.apple.mpegurl",
                        headers={"Cache-Control": "no-cache"})
    return await routes.stream_channel(channel_id)
