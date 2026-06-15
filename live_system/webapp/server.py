"""Relive Studio — FastAPI backend (dashboard tool).

Bọc lõi sẵn có (live_controller/obs_controller/live_database) thành JSON API + WebSocket
realtime + phục vụ dashboard HTML. Chạy 1 LiveController + thread run_forever trong tiến trình này.

LƯU Ý: chỉ chạy MỘT engine điều khiển OBS tại một thời điểm — run_studio HOẶC run_live (Gradio),
vì cả hai đều có vòng lặp 24/7 đẩy video lên OBS. Cả hai dùng chung live.db nên dữ liệu liên thông.
"""
import asyncio
import base64
import json
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))  # cho phép import live_* trong live_system/

from fastapi import Body, FastAPI, File, Form, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import live_database as db
from live_controller import LiveController, SCENE_MAIN, SRC_IMAGE, SRC_VIDEO
from live_scheduler import LiveScheduler
from obs_controller import OBSController

# ---- khởi tạo engine ----
controller = LiveController()
threading.Thread(target=controller.run_forever, daemon=True).start()
scheduler = LiveScheduler(controller)
threading.Thread(target=scheduler.run_forever, daemon=True).start()

# Kết nối OBS RIÊNG cho giám sát (chụp ảnh preview + đọc số liệu) — KHÔNG dùng chung lock với
# kết nối điều khiển/phát video, để lệnh Next/đổi scene... không phải chờ sau ảnh chụp (giảm trễ ~5s).
monitor_obs = OBSController(host=controller.obs.host, port=controller.obs.port,
                            password=controller.obs.password)

# Khớp comment → sản phẩm (keyword + AI). Gắn controller để trigger phát video/ghim SP,
# và nối hook vào scanner để mỗi comment quét được tự động khớp + (tùy chọn) trigger.
from comment_handler import handler as comment_handler  # noqa: E402
from shopee_scanner import scanner as shopee_scanner_singleton  # noqa: E402

comment_handler.controller = controller
shopee_scanner_singleton.on_comment = lambda c: comment_handler.handle(c)

# Video đang phát nói SP nào -> tự ghim SP đó trên Shopee (dùng session_id của phiên đang quét).
def _pin_current_product(product):
    import shopee_api
    shopee_api.pin_product(product, shopee_scanner_singleton.session_id,
                           shopee_scanner_singleton.code or "VN")
controller.on_product_change = _pin_current_product


# Khi scanner khóa được phiên live mới -> tự đồng bộ SP + khớp lại video import sẵn (match-after-live).
def _auto_sync_rematch(session_id):
    import logging
    import shopee_api
    log = logging.getLogger("live")
    try:
        rep = shopee_api.sync_products_from_live(session_id, shopee_scanner_singleton.code or "VN")
        n = _rematch_unmatched()
        log.info(f"[Auto] phiên {session_id}: đồng bộ SP {rep} + khớp lại {n} video")
    except Exception as e:
        log.warning(f"[Auto] sync/rematch lỗi: {e}")
shopee_scanner_singleton.on_live_session = _auto_sync_rematch


# Tự BẬT scanner khi khởi động nếu đã có cookie (khỏi quên bấm "Bắt đầu quét" trước khi live).
def _autostart_scanner():
    import logging
    log = logging.getLogger("live")
    try:
        rows = db.list_shopee_cookies()
        if rows:
            shopee_scanner_singleton.start(rows[0]["code"])
            log.info(f"[Auto] scanner tự bật lúc khởi động (code={rows[0]['code']})")
        else:
            log.info("[Auto] chưa có cookie — scanner chờ, bật lại sau khi extension đẩy cookie")
    except Exception as e:
        log.warning(f"[Auto] không tự bật được scanner: {e}")
_autostart_scanner()

app = FastAPI(title="Relive Studio")

# CORS mở cho mọi origin — cần cho extension (chrome-extension://...) POST cookie vào /api/shopee/cookie.
# Đây là công cụ chạy nội bộ trên localhost nên chấp nhận được.
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])


@app.middleware("http")
async def _no_cache_html(request, call_next):
    """Không cache trang HTML (luôn lấy mới) — file tĩnh JS/CSS vẫn cache nhờ ?v=mtime."""
    resp = await call_next(request)
    if resp.headers.get("content-type", "").startswith("text/html"):
        resp.headers["Cache-Control"] = "no-store"
    return resp
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
templates = Jinja2Templates(directory=str(HERE / "templates"))

ROOT = HERE.parent.parent                 # repo root
DOWNLOADS_DIR = ROOT / "downloads"
IMAGES_DIR = HERE.parent / "product_images"
IMAGES_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_TMP = HERE / "_uploads"
UPLOAD_TMP.mkdir(parents=True, exist_ok=True)
THUMBS_DIR = HERE / "_thumbs"
THUMBS_DIR.mkdir(parents=True, exist_ok=True)
ASSETS_DIR = HERE / "_assets"
ASSETS_DIR.mkdir(parents=True, exist_ok=True)

# (key, label, icon, route, built?)
# Chỉ giữ các tab đang hoạt động. Kịch bản AI đã là subtab trong Playlist nên bỏ khỏi sidebar.
# Các tab placeholder (Lịch live, Phiên live, Thống kê, Scene, Nguồn, Media, Cài đặt) đã gỡ —
# route vẫn còn (vào thẳng URL được), chỉ ẩn khỏi điều hướng. Thêm lại = 1 dòng.
SIDEBAR = [
    ("dashboard", "Dashboard", "📊", "/", True),
    ("comments", "Comment live", "💬", "/comments", True),
    ("playlist", "Playlist video", "🎞️", "/playlist", True),
    ("products", "Sản phẩm", "🛍️", "/products", True),
    ("assets", "Scene Assets", "🎨", "/assets", True),
    ("rules", "Quy tắc phát", "⚙️", "/rules", True),
]


def _asset_ver():
    """Phiên bản file tĩnh = mtime mới nhất trong static/ — để cache-bust JS/CSS, ép browser tải mới."""
    try:
        return str(int(max(p.stat().st_mtime for p in (HERE / "static").glob("*"))))
    except Exception:
        return "1"


def _ctx(request, page):
    return {"request": request, "sidebar": SIDEBAR, "page": page, "v": _asset_ver()}


def _duration(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30)
        return float(out.stdout.strip())
    except Exception:
        return 0.0


def _playlist(pl_id=None):
    out = []
    for i, e in enumerate(db.get_playlist(pl_id), 1):
        out.append({"idx": i, "playlist_id": e.playlist_id, "video_id": e.video_id,
                    "video": Path(e.file_path).name,
                    "product": e.product_name or "—", "duration": e.duration,
                    "is_played": bool(e.is_played)})
    return out


def _mask_key(k):
    if not k:
        return ""
    return ("•" * max(0, len(k) - 4)) + k[-4:] if len(k) > 4 else "••••"


def _session_view(s, mask=True):
    if not s:
        return None
    return {"id": s.id, "name": s.name, "platform": s.platform, "rtmp_server": s.rtmp_server,
            "stream_key": _mask_key(s.stream_key) if mask else s.stream_key,
            "scene": s.scene, "start_at": s.start_at, "end_at": s.end_at,
            "auto_start": s.auto_start, "auto_stop": s.auto_stop, "auto_recover": s.auto_recover,
            "pl_id": getattr(s, "pl_id", None), "profile": getattr(s, "profile", None),
            "status": s.status, "error": s.error, "started_at": s.started_at, "ended_at": s.ended_at}


_MODE_LABEL = {"order": "Theo thứ tự", "random": "Ngẫu nhiên", "priority": "Ưu tiên",
               "commission": "Hoa hồng cao", "sale": "SP sale"}


def build_status():
    s = controller.get_status(monitor_obs)   # đọc OBS qua kết nối giám sát riêng
    if s.get("product") and s["product"].get("image_path"):
        s["product"]["image"] = _img_url(s["product"]["id"], s["product"]["image_path"])
    # Dashboard hiển thị đúng playlist đang phát (theo phiên live, hoặc mặc định).
    pid = controller.active_pl_id or db.get_default_playlist_id()
    s["playlist"] = _playlist(pid)
    meta = db.get_playlist_meta(pid)
    if meta:
        s["playlist_id"] = pid
        s["playlist_name"] = meta.name
        s["playlist_mode"] = _MODE_LABEL.get(meta.play_mode, meta.play_mode)
        s["playlist_mode_raw"] = meta.play_mode
        s["playlist_group"] = meta.group_filter or ""
        s["playlist_autoplay"] = bool(meta.autoplay)
        s["playlist_loop"] = bool(meta.loop)
    sess = db.get_active_session() or db.get_next_scheduled_session()
    s["session"] = _session_view(sess, mask=True)
    try:
        s["rotate_status"] = controller.rotator.status()   # đếm ngược tự-đổi asset cho UI
    except Exception:
        s["rotate_status"] = {}
    return s


# ---- cache nền: gom mọi OBS call vào 1 thread; WS/endpoint chỉ đọc cache (không chặn event loop) ----
_status_cache = {"obs_connected": False, "playlist": [], "logs": [], "stats": {}, "stream": {}}
_preview_cache = b""
_cache_lock = threading.Lock()

def _poller():
    global _preview_cache
    while True:
        try:
            st = build_status()
            with _cache_lock:
                _status_cache.clear()
                _status_cache.update(st)
        except Exception:
            pass
        try:
            data_url = monitor_obs.get_program_screenshot(640)
            if data_url and "," in data_url:
                _preview_cache = base64.b64decode(data_url.split(",", 1)[1])
        except Exception:
            pass
        time.sleep(1.2)


threading.Thread(target=_poller, daemon=True).start()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("dashboard.html", _ctx(request, "dashboard"))


@app.get("/playlist", response_class=HTMLResponse)
def page_playlist(request: Request):
    return templates.TemplateResponse("playlist.html", _ctx(request, "playlist"))


_BUILT = {"dashboard", "playlist", "products", "schedule", "scripts", "rules", "assets", "comments"}

DEFAULT_SCENE_RANDOM = {
    "random_bg": True, "random_banner": True, "random_tvc": True, "random_table": True,
    "seed_mode": "auto", "seed_value": "",
    # Tự đổi asset theo thời gian (giây) khi đang stream — bg/banner/tvc độc lập.
    "background_rotate_enabled": False, "background_rotate_interval": 300,
    "banner_rotate_enabled": False, "banner_rotate_interval": 300,
    "table_rotate_enabled": False, "table_rotate_interval": 300,
    "tvc_rotate_enabled": False, "tvc_rotate_interval": 300,
}
_IMG = (".jpg", ".jpeg", ".png", ".webp", ".gif")
_ASSET_EXT = {"background": _IMG,
              "banner": _IMG,
              "table": _IMG,    # bàn + sản phẩm (ảnh, nên dùng PNG nền trong suốt)
              # TVC: ảnh HOẶC video
              "tvc": (".mp4", ".mov", ".mkv", ".webm", ".avi") + _IMG}

# Quy tắc phát mặc định (mục 8). Lưu trong settings['rules'].
DEFAULT_RULES = {
    "play_limit_enabled": False,   # tôn trọng cột play_limit của video (số lần tối đa/phiên)
    "follow_every_n": 0,           # sau N video playlist → chèn 1 video category 'follow' (0=tắt)
    "follow_category": "follow",
    "voucher_every_min": 0,        # sau X phút → chèn video category 'voucher' (0=tắt)
    "voucher_category": "voucher",
    "top_every_min": 0,            # sau X phút → phát lại top SP hoa hồng cao (0=tắt)
    "top_count": 3,
}


@app.get("/{page}", response_class=HTMLResponse)
def page_generic(request: Request, page: str):
    label = next((lb for k, lb, *_ in SIDEBAR if k == page), page)
    if page in _BUILT:
        return templates.TemplateResponse(f"{page}.html", _ctx(request, page))
    return templates.TemplateResponse("coming_soon.html", {**_ctx(request, page), "label": label})


# ---------------------------------------------------------------- Playlist/Video API

@app.get("/api/videos")
def api_videos():
    return {"videos": [{"id": v.id, "name": v.name or Path(v.file_path).name,
                        "file": Path(v.file_path).name, "product_id": v.product_id,
                        "product": v.product_name or "", "group": v.group_name or "",
                        "category": getattr(v, "category", None) or "",
                        "priority": v.priority, "duration": v.duration,
                        "play_count": v.play_count, "is_error": bool(v.is_error)}
                       for v in db.get_all_videos()]}


@app.get("/api/video_thumb/{video_id}")
def api_video_thumb(video_id: int):
    """Ảnh thumbnail của video (trích 1 frame bằng ffmpeg, cache theo mtime file)."""
    v = db.get_video(video_id)
    if not v or not v.file_path or not Path(v.file_path).exists():
        return Response(status_code=404)
    src = Path(v.file_path)
    out = THUMBS_DIR / f"{video_id}.jpg"
    try:
        if not out.exists() or out.stat().st_mtime < src.stat().st_mtime:
            subprocess.run(
                ["ffmpeg", "-y", "-ss", "1", "-i", str(src), "-frames:v", "1",
                 "-vf", "scale=160:-1", str(out)],
                capture_output=True, timeout=30)
    except Exception:
        pass
    if out.exists():
        return FileResponse(str(out), media_type="image/jpeg")
    return Response(status_code=404)


@app.get("/api/downloads")
def api_downloads():
    if not DOWNLOADS_DIR.exists():
        return {"files": []}
    return {"files": sorted(p.name for p in DOWNLOADS_DIR.glob("*.mp4"))}


@app.post("/api/videos/add")
def api_video_add(payload: dict = Body(default={})):
    fname = payload.get("file")
    if not fname:
        return JSONResponse({"ok": False, "error": "thiếu file"}, status_code=400)
    fpath = DOWNLOADS_DIR / fname
    if not fpath.exists():
        return JSONResponse({"ok": False, "error": "không thấy file"}, status_code=400)
    extra = {}
    if payload.get("category"):
        extra["category"] = payload["category"]
    pid = payload.get("product_id") or None
    if pid:
        extra["match_status"] = "confirmed"
    else:   # không chọn SP -> tự resolve theo sidecar/tên file
        pid, score, status, cand = db.resolve_product_for_video(str(fpath))
        extra.update(match_status=status, match_score=score, match_candidates=cand)
    vid = db.add_video(str(fpath), pid, _duration(fpath), **extra)
    return {"ok": True, "id": vid, "product_id": pid, "match_status": extra.get("match_status")}


_VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".flv")


VIDEOS_DIR = HERE.parent / "videos"     # nơi lưu video import (tách khỏi downloads/ của hệ render)
VIDEOS_DIR.mkdir(parents=True, exist_ok=True)


@app.post("/api/videos/upload")
async def api_video_upload(files: list[UploadFile] = File(...)):
    """Tải NHIỀU video lên + tự define SP theo TÊN file ('<sp>__<INTENT>.mp4').
    Đặt tên chuẩn -> tự gắn product (item_id/fuzzy) + intent (answer_videos)."""
    if not files:
        return JSONResponse({"ok": False, "error": "thiếu file"}, status_code=400)
    rep = {"added": 0, "linked": 0, "answer": 0, "review": 0, "unmatched": 0, "skipped": 0, "errors": 0}
    for f in files:
        if not f or not f.filename or Path(f.filename).suffix.lower() not in _VIDEO_EXTS:
            rep["errors"] += 1
            continue
        dst = VIDEOS_DIR / Path(f.filename).name
        if dst.exists():
            # Chèn hậu tố chống trùng TRƯỚC '__INTENT' để không phá quy ước tên.
            stem, suf = dst.stem, dst.suffix
            h = uuid.uuid4().hex[:6]
            if "__" in stem:
                head, _, tail = stem.rpartition("__")
                dst = VIDEOS_DIR / f"{head}_{h}__{tail}{suf}"
            else:
                dst = VIDEOS_DIR / f"{stem}_{h}{suf}"
        with open(dst, "wb") as out:
            shutil.copyfileobj(f.file, out)
        # parse theo TÊN GỐC (Path(f.filename).stem) để hậu tố chống trùng không lẫn vào tên SP
        r = db.import_video_by_name(str(dst), _duration(dst), name_hint=Path(f.filename).stem)
        if r.get("skipped"):
            rep["skipped"] += 1
            continue
        rep["added"] += 1
        if r.get("kind") == "answer":
            rep["answer"] += 1
        if r.get("product_id"):
            rep["linked"] += 1
        if r.get("status") == "review":
            rep["review"] += 1
        elif r.get("status") == "unmatched":
            rep["unmatched"] += 1
    return {"ok": True, **rep}


@app.post("/api/videos/delete")
def api_video_delete(payload: dict = Body(default={})):
    v = db.get_video(int(payload["id"]))
    db.delete_video(int(payload["id"]))
    if v and v.file_path:
        db.delete_answers_by_path(v.file_path)   # xóa video -> mất luôn trong Kịch bản AI (cùng file)
    return {"ok": True}


@app.post("/api/videos/update")
def api_video_update(payload: dict = Body(default={})):
    vid = int(payload.pop("id"))
    fields = {k: payload[k] for k in payload}
    # Gán SP tay -> xác nhận; gỡ SP (về null) -> quay lại 'unmatched' (hiện cảnh báo trở lại).
    if "product_id" in fields:
        fields.setdefault("match_status", "confirmed" if fields["product_id"] else "unmatched")
    db.update_video(vid, **fields)
    return {"ok": True}


@app.post("/api/videos/error")
def api_video_error(payload: dict = Body(default={})):
    db.mark_video_error(int(payload["id"]), bool(payload.get("flag", True)))
    return {"ok": True}


@app.post("/api/videos/to_answer")
def api_video_to_answer(payload: dict = Body(default={})):
    """Chuyển 1 video THƯ VIỆN (giới thiệu, tab Playlist) -> VIDEO TRẢ LỜI của 1 intent (tab Kịch bản AI).
    Giữ nguyên SP + điểm khớp; xóa bản ghi video (FK ON DELETE CASCADE tự gỡ khỏi playlist)."""
    v = db.get_video(int(payload["id"]))
    if not v:
        return JSONResponse({"ok": False, "error": "không thấy video"}, status_code=400)
    try:
        intent_id = int(payload["intent_id"])
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "thiếu intent"}, status_code=400)
    if not db.get_intent(intent_id):
        return JSONResponse({"ok": False, "error": "intent không tồn tại"}, status_code=400)
    aid = db.add_answer_video(intent_id, v.file_path, name=v.name or Path(v.file_path).name,
                              duration=v.duration, product_id=v.product_id)
    db.update_answer_video(aid, match_status=getattr(v, "match_status", None) or "confirmed",
                           match_score=getattr(v, "match_score", 0) or 0,
                           match_candidates=getattr(v, "match_candidates", None))
    db.delete_video(v.id)
    return {"ok": True, "answer_id": aid}


@app.post("/api/answers/to_video")
def api_answer_to_video(payload: dict = Body(default={})):
    """Chuyển 1 VIDEO TRẢ LỜI -> video THƯ VIỆN (giới thiệu, tab Playlist). Giữ SP + điểm khớp."""
    a = db.get_answer_video(int(payload["id"]))
    if not a:
        return JSONResponse({"ok": False, "error": "không thấy video trả lời"}, status_code=400)
    vid = db.add_video(a.file_path, a.product_id, a.duration, name=a.name or Path(a.file_path).name,
                       match_status=getattr(a, "match_status", None) or "confirmed",
                       match_score=getattr(a, "match_score", 0) or 0,
                       match_candidates=getattr(a, "match_candidates", None))
    db.delete_answer_video(a.id)
    return {"ok": True, "video_id": vid}


def _pl_id(payload):
    """Lấy pl_id từ payload (None/rỗng -> playlist mặc định)."""
    v = payload.get("pl_id")
    return int(v) if v else None


@app.get("/api/playlists")
def api_playlists():
    """Danh sách playlist (nhóm) + chế độ phát + lọc nhóm + id mặc định + danh sách nhóm SP."""
    return {"playlists": [{"id": p.id, "name": p.name, "count": p.count,
                           "play_mode": p.play_mode or "order", "group_filter": p.group_filter or "",
                           "autoplay": bool(p.autoplay), "loop": bool(p.loop)}
                          for p in db.list_playlists()],
            "default_id": db.get_default_playlist_id(),
            "groups": db.get_product_groups()}


@app.post("/api/playlists/add")
def api_playlists_add(payload: dict = Body(default={})):
    name = (payload.get("name") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "Cần tên playlist"}, status_code=400)
    return {"ok": True, "id": db.add_playlist(name)}


@app.post("/api/playlists/rename")
def api_playlists_rename(payload: dict = Body(default={})):
    db.rename_playlist(int(payload["id"]), payload.get("name"))
    return {"ok": True}


@app.post("/api/playlists/update")
def api_playlists_update(payload: dict = Body(default={})):
    """Cập nhật chế độ phát (play_mode) và/hoặc lọc nhóm (group_filter) của 1 playlist."""
    fields = {k: payload[k] for k in ("name", "play_mode", "group_filter") if k in payload}
    for k in ("autoplay", "loop"):
        if k in payload:
            fields[k] = int(bool(payload[k]))
    db.update_playlist(int(payload["id"]), **fields)
    return {"ok": True}


@app.post("/api/playlists/delete")
def api_playlists_delete(payload: dict = Body(default={})):
    ok = db.delete_playlist(int(payload["id"]))
    return {"ok": bool(ok), "error": None if ok else "Không thể xóa playlist cuối cùng"}


@app.get("/api/playlist")
def api_playlist(pl_id: int = None):
    return {"playlist": _playlist(pl_id)}


@app.post("/api/playlist/add")
def api_playlist_add(payload: dict = Body(default={})):
    ok = db.add_to_playlist(int(payload["video_id"]), _pl_id(payload))
    return {"ok": bool(ok), "error": None if ok else "Không thêm 2 video giống nhau liên tiếp"}


@app.post("/api/playlist/remove")
def api_playlist_remove(payload: dict = Body(default={})):
    db.remove_from_playlist(int(payload["playlist_id"]))
    return {"ok": True}


@app.post("/api/playlist/clear")
def api_playlist_clear(payload: dict = Body(default={})):
    db.clear_playlist(_pl_id(payload))
    return {"ok": True}


@app.post("/api/playlist/reorder")
def api_playlist_reorder(payload: dict = Body(default={})):
    db.reorder_playlist([int(x) for x in payload.get("order", [])])
    return {"ok": True}


# ---------------------------------------------------------------- Sessions API (P2)

def _norm_dt(v):
    """datetime-local 'YYYY-MM-DDTHH:MM' -> 'YYYY-MM-DD HH:MM:SS'."""
    if not v:
        return None
    v = str(v).replace("T", " ").strip()
    return v + ":00" if len(v) == 16 else v


@app.get("/api/sessions")
def api_sessions():
    active = db.get_active_session()
    return {"sessions": [_session_view(s, mask=True) for s in db.list_sessions()],
            "active_id": active.id if active else None}


@app.post("/api/sessions/save")
def api_session_save(payload: dict = Body(default={})):
    if not (payload.get("name") or "").strip():
        return JSONResponse({"ok": False, "error": "Cần tên phiên"}, status_code=400)
    fields = dict(
        name=payload["name"].strip(), platform=payload.get("platform"),
        rtmp_server=payload.get("rtmp_server"),
        scene=payload.get("scene"), start_at=_norm_dt(payload.get("start_at")),
        end_at=_norm_dt(payload.get("end_at")),
        auto_start=int(bool(payload.get("auto_start", True))),
        auto_stop=int(bool(payload.get("auto_stop", True))),
        auto_recover=int(bool(payload.get("auto_recover", True))),
        pl_id=(int(payload["pl_id"]) if payload.get("pl_id") else None),
        profile=(payload.get("profile") or None),
    )
    # stream_key: chỉ set khi có giá trị (trống lúc sửa = giữ key cũ).
    if payload.get("stream_key"):
        fields["stream_key"] = payload["stream_key"]
    sid = payload.get("id")
    if sid:
        db.update_session(int(sid), **fields)
        return {"ok": True, "id": int(sid), "mode": "update"}
    fields["status"] = "scheduled"
    new_id = db.add_session(fields.pop("name"), **fields)
    return {"ok": True, "id": new_id, "mode": "add"}


@app.post("/api/sessions/delete")
def api_session_delete(payload: dict = Body(default={})):
    db.delete_session(int(payload["id"]))
    return {"ok": True}


@app.post("/api/sessions/{sid}/{action}")
def api_session_action(sid: int, action: str):
    if action == "start":
        ok = scheduler.start_session(sid)
        return {"ok": ok, "error": None if ok else "Đã có phiên đang live (dừng phiên đó trước)"}
    if action == "stop":
        return {"ok": scheduler.stop_session(sid)}
    if action == "cancel":
        return {"ok": scheduler.cancel_session(sid)}
    if action == "reschedule":  # đưa phiên về 'scheduled' để chạy lại theo giờ
        db.set_session_status(sid, "scheduled")
        return {"ok": True}
    return JSONResponse({"ok": False, "error": "unknown action"}, status_code=400)


@app.get("/api/rules")
def api_rules():
    """Quy tắc phát hiện tại (mặc định + đã lưu)."""
    saved = db.get_setting("rules", {}) or {}
    return {"rules": {**DEFAULT_RULES, **saved}}


@app.post("/api/rules/save")
def api_rules_save(payload: dict = Body(default={})):
    cur = {**DEFAULT_RULES, **(db.get_setting("rules", {}) or {})}
    for k in DEFAULT_RULES:
        if k in payload:
            cur[k] = payload[k]
    db.set_setting("rules", cur)
    return {"ok": True, "rules": cur}


@app.get("/api/status")
def api_status():
    with _cache_lock:
        return JSONResponse(dict(_status_cache))


@app.get("/api/preview.jpg")
def api_preview():
    if not _preview_cache:
        return Response(status_code=204)
    return Response(content=_preview_cache, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/scenes")
def api_scenes():
    return {"scenes": controller.obs.list_scenes(),
            "current": controller._safe(
                lambda: controller.obs.client.get_current_program_scene().current_program_scene_name)}


@app.get("/api/profiles")
def api_profiles():
    return {"profiles": controller.obs.get_profiles()}


# ---------------------------------------------------------------- Scene Assets (nền/banner/tvc + random)

def _asset_url(a):
    try:
        ver = int(Path(a.file_path).stat().st_mtime)
    except Exception:
        ver = 0
    return f"/api/asset_file/{a.id}?v={ver}"


_VIDEO_SUFFIX = (".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".flv")


@app.get("/api/assets")
def api_assets(kind: str = None):
    return {"assets": [{"id": a.id, "kind": a.kind, "name": a.name,
                        "enabled": bool(a.enabled), "url": _asset_url(a),
                        "is_video": Path(a.file_path).suffix.lower() in _VIDEO_SUFFIX}
                       for a in db.list_scene_assets(kind)],
            "applied": controller._applied}


@app.post("/api/assets/upload")
async def api_asset_upload(kind: str = Form(...), file: UploadFile = File(...), name: str = Form("")):
    if kind not in _ASSET_EXT:
        return JSONResponse({"ok": False, "error": "kind không hợp lệ"}, status_code=400)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in _ASSET_EXT[kind]:
        return JSONResponse({"ok": False, "error": f"{kind} chỉ nhận {', '.join(_ASSET_EXT[kind])}"},
                            status_code=400)
    dst = ASSETS_DIR / f"{kind}_{uuid.uuid4().hex[:10]}{ext}"
    with open(dst, "wb") as f:
        shutil.copyfileobj(file.file, f)
    aid = db.add_scene_asset(kind, str(dst.resolve()), name or Path(file.filename).stem)
    try:
        controller.apply_current_assets()   # tự đẩy lên OBS ngay (khỏi cần bấm Apply)
    except Exception:
        pass
    return {"ok": True, "id": aid}


@app.post("/api/assets/toggle")
def api_asset_toggle(payload: dict = Body(default={})):
    db.toggle_scene_asset(int(payload["id"]), bool(payload.get("enabled", True)))
    try:
        controller.apply_current_assets()   # bật/tắt là cập nhật OBS ngay
    except Exception:
        pass
    return {"ok": True}


@app.post("/api/assets/delete")
def api_asset_delete(payload: dict = Body(default={})):
    a = db.get_scene_asset(int(payload["id"]))
    if a and a.file_path:
        Path(a.file_path).unlink(missing_ok=True)
    db.delete_scene_asset(int(payload["id"]))
    return {"ok": True}


@app.get("/api/asset_file/{aid}")
def api_asset_file(aid: int):
    a = db.get_scene_asset(aid)
    if a and a.file_path and Path(a.file_path).exists():
        return FileResponse(a.file_path, headers={"Cache-Control": "no-cache"})
    return Response(status_code=404)


@app.get("/api/scene/random_settings")
def api_scene_random_get():
    return {"settings": {**DEFAULT_SCENE_RANDOM, **(db.get_setting("scene_random", {}) or {})}}


@app.post("/api/scene/random_settings")
def api_scene_random_save(payload: dict = Body(default={})):
    cur = {**DEFAULT_SCENE_RANDOM, **(db.get_setting("scene_random", {}) or {})}
    for k in DEFAULT_SCENE_RANDOM:
        if k in payload:
            cur[k] = payload[k]
    db.set_setting("scene_random", cur)
    controller.rotator.reload()   # áp cấu hình tự-đổi mới ngay (nếu đang stream)
    return {"ok": True, "settings": cur}


def _pick_urls(pick):
    """Gắn URL hiển thị cho bg/banner/tvc trong pick (cho preview)."""
    if not pick:
        return pick
    for k in ("background", "banner", "tvc"):
        a = pick.get(k)
        if a and a.get("id"):
            rec = db.get_scene_asset(a["id"])
            if rec:
                a["url"] = _asset_url(rec)
    return pick


@app.post("/api/scene/random_preview")
def api_scene_random_preview():
    return {"pick": _pick_urls(controller.compute_scene_random())}


@app.post("/api/scene/apply")
def api_scene_apply(payload: dict = Body(default={})):
    pick = payload.get("pick")
    if pick:   # re-resolve file path từ id (không tin path do client gửi)
        for k in ("background", "banner", "tvc"):
            a = pick.get(k)
            if a and a.get("id"):
                rec = db.get_scene_asset(a["id"])
                pick[k] = {"id": rec.id, "name": rec.name, "file_path": rec.file_path} if rec else None
            else:
                pick[k] = None
    applied = controller.apply_scene_random(pick)
    if not applied:
        return {"ok": False, "error": "Chưa có asset nào đang Bật để áp dụng"}
    results = applied.get("results", {})
    ok = bool(results) and all(results.values())
    return {"ok": ok, "results": results, "message": applied.get("message"),
            "pick": _pick_urls(applied)}


@app.post("/api/scene/apply_one")
def api_scene_apply_one(payload: dict = Body(default={})):
    """Áp đúng 1 asset người dùng click chọn lên OBS."""
    applied = controller.apply_one_asset(int(payload["id"]))
    if not applied:
        return JSONResponse({"ok": False, "error": "không áp được"}, status_code=400)
    results = applied.get("results", {})
    return {"ok": bool(results) and all(results.values())}


@app.post("/api/scene/ensure_sources")
def api_scene_ensure():
    ok = controller.obs.ensure_live_sources()
    return {"ok": bool(ok)}


@app.post("/api/scene/reset_layout")
def api_scene_reset_layout():
    """Đặt lại vị trí/kích thước mặc định (chỉ khi bấm tay — random không đụng vị trí)."""
    ok = controller.obs.apply_default_layout(SCENE_MAIN)
    return {"ok": bool(ok)}


def _img_url(pid, image_path):
    """URL ảnh SP kèm ?v=mtime để đổi ảnh là URL đổi -> trình duyệt tải ảnh mới (không dính cache)."""
    if not image_path:
        return None
    try:
        ver = int(Path(image_path).stat().st_mtime)
    except Exception:
        ver = 0
    return f"/api/product_image/{pid}?v={ver}"


def _product_dict(p):
    return {"id": p.id, "name": p.name, "link": getattr(p, "link", None),
            "shopee_item_id": getattr(p, "shopee_item_id", None),
            "shop_id": getattr(p, "shop_id", None),
            "price": getattr(p, "price", None), "sold": getattr(p, "sold", None),
            "stock": getattr(p, "stock", None),
            "image_path": p.image_path, "image": _img_url(p.id, p.image_path)}


@app.get("/api/products")
def api_products():
    return {"products": [_product_dict(p) for p in db.get_all_products()]}


@app.get("/api/product_image/{pid}")
def api_product_image(pid: int):
    p = db.get_product(pid)
    if p and p.image_path and Path(p.image_path).exists():
        return FileResponse(p.image_path, headers={"Cache-Control": "no-cache"})
    return Response(status_code=404)


def _save_upload(upload):
    if not upload or not upload.filename:
        return None
    dst = IMAGES_DIR / f"{uuid.uuid4().hex[:10]}{Path(upload.filename).suffix}"
    with open(dst, "wb") as f:
        shutil.copyfileobj(upload.file, f)
    return str(dst)


def _num(v):
    if v in (None, ""):
        return None
    try:
        return float(v)
    except ValueError:
        return None


@app.post("/api/products/save")
async def api_product_save(id: str = Form(""), name: str = Form(...),
                           link: str = Form(""), item_id: str = Form(""),
                           image: UploadFile = File(None)):
    """Thêm/sửa sản phẩm: tên, link, ảnh + shopee_item_id (item_id phiên live để ghim)."""
    if not name.strip():
        return JSONResponse({"ok": False, "error": "Cần tên sản phẩm"}, status_code=400)
    img = _save_upload(image)
    iid = item_id.strip() or None
    if id and id.isdigit():
        fields = dict(name=name.strip(), link=link or None, shopee_item_id=iid)
        if img:
            fields["image_path"] = img
        db.update_product(int(id), **fields)
        return {"ok": True, "id": int(id), "mode": "update"}
    pid = db.add_product(name.strip(), img, link or None)
    if iid:
        db.update_product(pid, shopee_item_id=iid)
    return {"ok": True, "id": pid, "mode": "add"}


@app.post("/api/products/delete")
def api_product_delete(payload: dict = Body(default={})):
    db.delete_product(int(payload["id"]))
    return {"ok": True}


@app.get("/api/products/current")
def api_current_product():
    """SP đang phát (gắn với video hiện tại) + thông tin sàn — cho extension auto-ghim."""
    p = controller.current_product_obj
    if not p:
        return {"product": None}
    return {"product": {"id": p.id, "name": p.name,
                        "platform_id": getattr(p, "platform_id", None),
                        "platform_link": getattr(p, "platform_link", None),
                        "platform": getattr(p, "platform", None)}}


def _pin_product(product_id):
    p = db.get_product(product_id)
    if not p:
        return False
    # Ghim SP: đổi ảnh SP trên OBS + cập nhật hiển thị dashboard.
    controller.obs.set_image(SRC_IMAGE, p.image_path or "")
    controller.current_product = p.name
    controller.current_product_obj = p
    return True


@app.post("/api/shopee/cookie")
def api_shopee_cookie(payload: dict = Body(default={})):
    """Nhận cookie + User-Agent từ extension EcomTools và lưu vào hệ thống (theo mã quốc gia)."""
    code = (payload.get("code") or payload.get("country") or "").strip().upper()
    cookie = payload.get("cookie") or ""
    if not code or not cookie:
        return JSONResponse({"ok": False, "error": "thiếu code hoặc cookie"}, status_code=400)
    try:
        row = db.save_shopee_cookie(code, cookie,
                                    domain=payload.get("domain"),
                                    user_agent=payload.get("user_agent"))
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return {"ok": True, "code": row["code"], "domain": row["domain"],
            "cookie_len": len(cookie), "updated_at": row["updated_at"]}


@app.get("/api/shopee/cookies")
def api_shopee_cookies():
    """Danh sách cookie Shopee đã lưu (metadata, không lộ chuỗi cookie đầy đủ)."""
    return {"cookies": db.list_shopee_cookies()}


_SEEN_ENDPOINTS = {}   # key "METHOD path" -> {method, path, query_keys, count, last}


@app.post("/api/shopee/seen")
def api_shopee_seen(payload: dict = Body(default={})):
    """Extension báo các endpoint API Shopee Creator nó thấy (để dò đường dẫn list/ghim SP)."""
    method = (payload.get("method") or "GET").upper()
    path = payload.get("path") or ""
    if not path:
        return {"ok": False}
    key = f"{method} {path}"
    rec = _SEEN_ENDPOINTS.get(key) or {"method": method, "path": path, "count": 0,
                                       "query_keys": payload.get("query_keys") or []}
    rec["count"] += 1
    rec["last_query"] = payload.get("query") or ""
    _SEEN_ENDPOINTS[key] = rec
    return {"ok": True}


@app.get("/api/shopee/seen")
def api_shopee_seen_list():
    """Danh sách endpoint Shopee Creator đã quan sát (sắp theo lần thấy gần nhất)."""
    return {"endpoints": sorted(_SEEN_ENDPOINTS.values(), key=lambda r: -r["count"])}


@app.post("/api/shopee/seen/clear")
def api_shopee_seen_clear():
    _SEEN_ENDPOINTS.clear()
    return {"ok": True}


@app.get("/api/triggers")
def api_triggers():
    """Danh sách trigger keyword→SP + danh sách SP (cho dropdown)."""
    prods = [{"id": p.id, "name": p.name} for p in db.get_all_products()]
    trigs = [{"id": t.id, "product_id": t.product_id, "keyword": t.keyword,
              "product_name": t.product_name} for t in db.get_all_triggers()]
    return {"triggers": trigs, "products": prods}


@app.post("/api/triggers/add")
def api_trigger_add(payload: dict = Body(default={})):
    """Thêm 1 HOẶC nhiều keyword (phân tách bằng dấu phẩy) cho 1 sản phẩm."""
    pid = int(payload["product_id"])
    kws = [k.strip() for k in (payload.get("keyword", "") or "").split(",") if k.strip()]
    if not kws:
        return JSONResponse({"ok": False, "error": "Nhập ít nhất 1 từ khóa"}, status_code=400)
    added = 0
    try:
        for kw in kws:
            db.add_trigger(pid, kw); added += 1
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    from intent_matcher import matcher
    matcher.reload()
    return {"ok": True, "added": added}


@app.post("/api/triggers/delete")
def api_trigger_delete(payload: dict = Body(default={})):
    db.delete_trigger(int(payload["id"]))
    from intent_matcher import matcher
    matcher.reload()
    return {"ok": True}


@app.get("/api/comment/ai_config")
def api_ai_config_get():
    from core import ai_helper
    return {
        "ai_enabled": db.get_setting("ai_enabled", True) is not False,
        "auto_trigger": db.get_setting("auto_trigger", True) is not False,
        "trigger_mode": db.get_setting("trigger_mode", "play_now"),
        "video_pick": db.get_setting("video_pick", "rotate"),
        "cooldown": int(db.get_setting("answer_cooldown_sec", 45)),
        "threshold": float(db.get_setting("ai_threshold", 0.85)),
        "model": db.get_setting("ai_model") or ai_helper.DEFAULT_MODEL,
        "has_key": ai_helper.is_configured(),     # KHÔNG trả key ra ngoài
    }


@app.post("/api/comment/ai_config")
def api_ai_config_set(payload: dict = Body(default={})):
    if "ai_enabled" in payload:
        db.set_setting("ai_enabled", bool(payload["ai_enabled"]))
    if "auto_trigger" in payload:
        db.set_setting("auto_trigger", bool(payload["auto_trigger"]))
    if payload.get("trigger_mode") in ("play_now", "enqueue"):
        db.set_setting("trigger_mode", payload["trigger_mode"])
    if payload.get("video_pick") in ("rotate", "random"):
        db.set_setting("video_pick", payload["video_pick"])
    if payload.get("cooldown") not in (None, ""):
        db.set_setting("answer_cooldown_sec", max(0, int(payload["cooldown"])))
    if "threshold" in payload and payload["threshold"] not in (None, ""):
        db.set_setting("ai_threshold", max(0.0, min(1.0, float(payload["threshold"]))))
    if payload.get("model"):
        db.set_setting("ai_model", str(payload["model"]).strip())
    if payload.get("api_key"):          # chỉ ghi khi có giá trị mới (tránh xóa nhầm)
        db.set_setting("ai_api_key", str(payload["api_key"]).strip())
    return {"ok": True, **api_ai_config_get()}


@app.post("/api/comment/test")
def api_comment_test(payload: dict = Body(default={})):
    """Ô TEST COMMENT: khớp + trả method/confidence/lý do/video.
    trigger=True → phát THẬT lên OBS (để kiểm tra chuyển video); mặc định chỉ xem trước."""
    text = payload.get("content") or payload.get("comment") or ""
    if not text.strip():
        return JSONResponse({"ok": False, "error": "Nhập comment"}, status_code=400)
    really = bool(payload.get("trigger"))
    res = comment_handler.handle({"content": text}, test_only=not really)
    return {"ok": True, **res}


@app.post("/api/comment/ingest")
def api_comment_ingest(payload: dict = Body(default={})):
    """Extension cào comment THẬT từ trang live gửi về → xử lý y hệt (khớp + trigger OBS + feed).
    Body: {"comments": [...]} (mảng comment thô từ API Shopee) hoặc 1 comment {"content":...}."""
    comments = payload.get("comments")
    if comments is None and payload.get("content"):
        comments = [payload]
    from shopee_scanner import scanner
    added = scanner.ingest_external(comments or [])
    return {"ok": True, "added": added}


@app.post("/api/comment/inject")
def api_comment_inject(payload: dict = Body(default={})):
    """Giả lập 1 comment LIVE (test local): khớp + trigger OBS thật + hiện trong feed comment."""
    text = (payload.get("content") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "Nhập comment"}, status_code=400)
    from shopee_scanner import scanner
    item = scanner.inject_local(text, payload.get("user") or "local-test")
    return {"ok": True, **(item.get("match") or {}), "seq": item.get("seq")}


@app.get("/api/comment/logs")
def api_comment_logs(limit: int = 100):
    rows = db.list_comment_logs(limit)
    return {"logs": [{
        "id": r.id, "content": r.content, "user_id": r.user_id,
        "product": r.product_name, "confidence": round(float(r.confidence) * 100),
        "method": r.match_method, "triggered": bool(r.triggered),
        "created_at": r.created_at} for r in rows]}


@app.post("/api/shopee/scan/start")
def api_scan_start(payload: dict = Body(default={})):
    """Bắt đầu quét comment: tự dò phiên live của tài khoản (theo code) rồi poll comment."""
    code = (payload.get("code") or "").strip().upper()
    if not code:
        rows = db.list_shopee_cookies()
        if not rows:
            return JSONResponse({"ok": False, "error": "Chưa có cookie nào. Lấy cookie qua extension trước."},
                                status_code=400)
        code = rows[0]["code"]
    from shopee_scanner import scanner
    scanner.start(code)
    return {"ok": True, **scanner.status()}


@app.post("/api/shopee/scan/stop")
def api_scan_stop():
    from shopee_scanner import scanner
    scanner.stop()
    return {"ok": True, **scanner.status()}


@app.get("/api/shopee/scan/status")
def api_scan_status():
    from shopee_scanner import scanner
    return scanner.status()


@app.get("/api/shopee/scan/comments")
def api_scan_comments(since: int = 0):
    from shopee_scanner import scanner
    return {"comments": scanner.comments_since(since), **scanner.status()}


@app.get("/api/shopee/items")
def api_shopee_items(code: str = "", session_id: int = 0):
    """Danh sách sản phẩm trong phiên live (item_id, shop_id, tên) — qua api.relive.vn."""
    import shopee_api
    from shopee_scanner import scanner
    code = (code or scanner.code or "VN").upper()
    sid = session_id or scanner.session_id
    if not sid:
        return JSONResponse({"ok": False, "error": "Chưa có phiên live (bật quét hoặc truyền session_id)"},
                            status_code=400)
    try:
        return {"ok": True, "session_id": sid, "items": shopee_api.get_live_items(sid, code, use_cache=False)}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


_GOOD = ("auto", "manifest", "item")


def _rematch_unmatched():
    """Chạy lại resolver cho video + answer_video review/unmatched (sau khi catalog có thêm SP).
    Trả số đã gắn được SP."""
    fixed = 0
    for v in db.list_review_videos():
        pid, score, status, cand = db.resolve_product_for_video(v.file_path, name_hint=v.name)
        if pid and status in _GOOD:
            db.update_video(v.id, product_id=pid, match_status=status,
                            match_score=score, match_candidates=cand)
            fixed += 1
        else:
            db.update_video(v.id, match_status=status, match_score=score, match_candidates=cand)
    for a in db.list_review_answers():
        pid, score, status, cand = db.resolve_product_for_video(a.file_path, name_hint=a.name)
        if pid and status in _GOOD:
            db.update_answer_video(a.id, product_id=pid, match_status=status,
                                   match_score=score, match_candidates=cand)
            fixed += 1
        else:
            db.update_answer_video(a.id, match_status=status, match_score=score, match_candidates=cand)
    return fixed


@app.get("/api/shopee/sessions")
def api_shopee_sessions(code: str = "VN", counts: int = 6):
    """Liệt kê phiên gần đây (mới->cũ) + số SP cho 'counts' phiên đầu — để UI chọn ĐÚNG phiên live
    (không phụ thuộc status chập chờn). Trả luôn phiên đang active của scanner."""
    import shopee_api
    from shopee_scanner import scanner
    code = (code or "VN").upper()
    try:
        sess = scanner.list_sessions(code)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    sess.sort(key=lambda s: (s.get("raw") or {}).get("startTime") or 0, reverse=True)
    out = []
    for i, s in enumerate(sess):
        raw = s.get("raw") or {}
        n = None
        if i < counts:
            try:
                n = len(shopee_api.get_live_items(s["session_id"], code, use_cache=False))
            except Exception:
                n = None
        out.append({"session_id": s["session_id"], "title": s.get("title") or "",
                    "status": s.get("status"),
                    "is_live": str(s.get("status")).lower() in ("1", "live", "ongoing", "streaming"),
                    "start_time": raw.get("startTime"), "n_products": n})
    return {"ok": True, "sessions": out, "active": scanner.session_id, "manual": scanner.manual}


@app.post("/api/shopee/sync_products")
def api_shopee_sync_products(payload: dict = Body(default={})):
    """Đồng bộ catalog products từ phiên live (theo shopee_item_id) + tự khớp lại video chưa gắn.
    Truyền session_id -> KHÓA phiên đó cho comment + ghim SP (không bị status chập chờn reset)."""
    import shopee_api
    from shopee_scanner import scanner
    code = (payload.get("code") or scanner.code or "VN").upper()
    sid = payload.get("session_id") or scanner.session_id
    if not sid:
        return JSONResponse({"ok": False, "error": "Chưa có phiên live (chọn phiên hoặc bật quét trước)"}, status_code=400)
    if payload.get("session_id"):
        scanner.use_session(sid, code)   # chọn tay -> khóa cứng phiên cho comment/ghim
    try:
        report = shopee_api.sync_products_from_live(sid, code, threshold=float(payload.get("threshold", 0.85)))
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    report["rematched"] = _rematch_unmatched()
    report["session_id"] = str(sid)
    return {"ok": True, **report}


@app.post("/api/videos/rematch")
def api_videos_rematch():
    """Khớp lại tất cả video review/unmatched (sau khi đã sync SP)."""
    return {"ok": True, "rematched": _rematch_unmatched()}


@app.get("/api/videos/review")
def api_videos_review():
    """Danh sách video chưa gắn SP chắc chắn + gợi ý top-3 (để duyệt tay)."""
    out = []
    for v in db.list_review_videos():
        try:
            cands = json.loads(v.match_candidates) if v.match_candidates else []
        except Exception:
            cands = []
        out.append({"id": v.id, "name": v.name or Path(v.file_path).name,
                    "file": Path(v.file_path).name, "status": v.match_status,
                    "score": round(float(v.match_score or 0) * 100), "candidates": cands})
    return {"videos": out}


@app.get("/api/answers/review")
def api_answers_review():
    """Video TRẢ LỜI (kịch bản AI) chưa gắn SP chắc chắn + gợi ý — duyệt tay, mọi intent."""
    out = []
    for a in db.list_review_answers():
        try:
            cands = json.loads(a.match_candidates) if a.match_candidates else []
        except Exception:
            cands = []
        it = db.get_intent(a.intent_id) if a.intent_id else None
        out.append({"id": a.id, "name": a.name or Path(a.file_path).name,
                    "file": Path(a.file_path).name, "intent": it.name if it else "",
                    "status": a.match_status,
                    "score": round(float(getattr(a, "match_score", 0) or 0) * 100), "candidates": cands})
    return {"answers": out}


@app.post("/api/shopee/match_products")
def api_shopee_match_products(payload: dict = Body(default={})):
    """Tự gán shopee_item_id cho SP bằng khớp TÊN với SP trong phiên live (fuzzy ≥ ngưỡng)."""
    import shopee_api
    from shopee_scanner import scanner
    code = (payload.get("code") or scanner.code or "VN").upper()
    sid = payload.get("session_id") or scanner.session_id
    if not sid:
        return JSONResponse({"ok": False, "error": "Chưa có phiên live (bật quét trước)"}, status_code=400)
    thr = float(payload.get("threshold", 0.85))
    try:
        report = shopee_api.auto_match_products(sid, code, threshold=thr,
                                                only_missing=bool(payload.get("only_missing")))
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return {"ok": True, "matched": sum(1 for r in report if r["set"]), "report": report}


@app.post("/api/shopee/pin")
def api_shopee_pin(payload: dict = Body(default={})):
    """Ghim 1 SP lên live. Body: {item_id, shop_id} hoặc {product_id} (tự tra item trong live)."""
    import shopee_api
    from shopee_scanner import scanner
    code = (payload.get("code") or scanner.code or "VN").upper()
    sid = payload.get("session_id") or scanner.session_id
    if not sid:
        return JSONResponse({"ok": False, "error": "Chưa có phiên live"}, status_code=400)
    item_id, shop_id = payload.get("item_id"), payload.get("shop_id")
    if payload.get("product_id") and not (item_id and shop_id):
        p = db.get_product(int(payload["product_id"]))
        it = shopee_api.find_item_for_product(sid, p, code) if p else None
        if not it:
            return JSONResponse({"ok": False, "error": "Không tìm thấy SP này trong phiên live"}, status_code=400)
        item_id, shop_id = it["item_id"], it["shop_id"]
    if not (item_id and shop_id):
        return JSONResponse({"ok": False, "error": "Thiếu item_id/shop_id"}, status_code=400)
    try:
        ok, msg = shopee_api.pin_item(sid, item_id, shop_id, code)
        return {"ok": ok, "message": msg}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.get("/api/shopee/sessions")
def api_shopee_sessions(code: str = ""):
    """Liệt kê phiên live của tài khoản (debug / chọn tay nếu auto chọn sai)."""
    from shopee_scanner import scanner, ShopeeAuthError
    code = (code or "").strip().upper()
    if not code:
        rows = db.list_shopee_cookies()
        if not rows:
            return JSONResponse({"ok": False, "error": "Chưa có cookie"}, status_code=400)
        code = rows[0]["code"]
    try:
        return {"ok": True, "code": code, "sessions": scanner.list_sessions(code)}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/shopee/cookie/delete")
def api_shopee_cookie_delete(payload: dict = Body(default={})):
    db.delete_shopee_cookie(payload.get("code", ""))
    return {"ok": True}


@app.post("/api/control/{action}")
def control(action: str, payload: dict = Body(default={})):
    ok = True
    if action == "start_stream":
        ok = controller.start_stream()
    elif action == "go_live":
        # Set đích RTMP (server + key của nền tảng) + playlist RỒI start — bắt đầu live ngay từ dashboard.
        server = (payload.get("rtmp_server") or "").strip()
        key = (payload.get("stream_key") or "").strip()
        if not server or not key:
            return {"ok": False, "error": "Cần nhập RTMP server và Stream key"}
        if payload.get("pl_id"):
            controller.set_active_playlist(int(payload["pl_id"]))
        if not controller.obs.set_stream_service(server, key):
            return {"ok": False, "error": "Không set được đích RTMP (kiểm tra OBS)"}
        ok = controller.start_stream()
        return {"ok": bool(ok), "error": None if ok else "OBS không start được stream"}
    elif action == "set_playlist":
        ok = controller.set_active_playlist(payload.get("pl_id") or None)
    elif action == "stop_stream":
        ok = controller.stop_stream()
    elif action == "start_record":
        ok = controller.start_recording()
    elif action == "stop_record":
        ok = controller.stop_recording()
    elif action == "next":
        ok = controller.next_video()
    elif action == "previous":
        ok = controller.previous_video()
    elif action == "toggle_pause":
        ok = controller.resume() if controller.is_paused() else controller.pause()
    elif action == "scene":
        ok = controller.set_scene(payload.get("name", SCENE_MAIN))
    elif action == "source_visible":
        ok = controller.set_source_visible(payload.get("source"), bool(payload.get("visible", True)))
    elif action == "volume":
        ok = controller.set_volume(payload.get("source"), float(payload.get("mul", 1.0)))
    elif action == "toggle_mute":
        ok = controller.toggle_mute(payload.get("source"))
    elif action == "reload_media":
        ok = controller.reload_media()
    elif action == "play_now":
        ok = controller.play_now(int(payload.get("video_id", 0)))
    elif action == "enqueue":
        ok = controller.enqueue(int(payload.get("video_id", 0)))
        return {"ok": bool(ok), "error": None if ok else "Trùng video liền trước trong hàng đợi"}
    elif action == "clear_queue":
        ok = controller.clear_queue()
    elif action == "pin":
        ok = _pin_product(int(payload.get("product_id", 0)))
    elif action == "unpin":
        controller.obs.set_image(SRC_IMAGE, "")   # xóa ảnh SP
        controller.current_product = None
        controller.current_product_obj = None
    else:
        return JSONResponse({"ok": False, "error": f"unknown action {action}"}, status_code=400)
    return {"ok": bool(ok)}


# ---------------------------------------------------------------- Intents + Answer videos (Phần B)

@app.get("/api/intents")
def api_intents():
    out = []
    for i in db.list_intents():
        ans = db.list_answer_videos(i.id)
        out.append({"id": i.id, "name": i.name, "keywords": i.keywords or "",
                    "trigger_mode": i.trigger_mode, "cooldown_sec": i.cooldown_sec,
                    "enabled": i.enabled, "answer_count": len(ans)})
    return {"intents": out}


@app.post("/api/intents/save")
def api_intent_save(payload: dict = Body(default={})):
    if not (payload.get("name") or "").strip():
        return JSONResponse({"ok": False, "error": "Cần tên intent"}, status_code=400)
    fields = dict(name=payload["name"].strip(), keywords=payload.get("keywords"),
                  trigger_mode=payload.get("trigger_mode", "enqueue"),
                  cooldown_sec=int(payload.get("cooldown_sec") or 30),
                  enabled=int(bool(payload.get("enabled", True))))
    iid = payload.get("id")
    if iid:
        db.update_intent(int(iid), **fields)
        return {"ok": True, "id": int(iid)}
    return {"ok": True, "id": db.add_intent(**fields)}


@app.post("/api/intents/delete")
def api_intent_delete(payload: dict = Body(default={})):
    db.delete_intent(int(payload["id"]))
    return {"ok": True}


@app.get("/api/answers")
def api_answers(intent_id: int = None):
    rows = db.list_answer_videos(intent_id)
    return {"answers": [{"id": a.id, "intent_id": a.intent_id, "intent": a.intent_name,
                         "name": a.name or Path(a.file_path).name, "file": Path(a.file_path).name,
                         "product": a.product_name or "", "product_id": a.product_id,
                         "duration": a.duration, "play_count": a.play_count,
                         "enabled": a.enabled, "last_played_at": a.last_played_at} for a in rows]}


@app.post("/api/answers/add")
def api_answer_add(payload: dict = Body(default={})):
    fname = payload.get("file")
    fpath = DOWNLOADS_DIR / fname if fname else None
    if not fpath or not fpath.exists():
        return JSONResponse({"ok": False, "error": "không thấy file trong downloads/"}, status_code=400)
    pid = payload.get("product_id") or None
    # Parity với api_video_add: chọn SP -> confirmed; không chọn -> tự khớp theo tên file.
    if pid:
        status, score, cand = "confirmed", 1.0, None
    else:
        pid, score, status, cand = db.resolve_product_for_video(str(fpath), name_hint=Path(fname).stem)
    aid = db.add_answer_video(int(payload["intent_id"]), str(fpath),
                              name=payload.get("name"), duration=_duration(fpath), product_id=pid)
    db.update_answer_video(aid, match_status=status, match_score=score, match_candidates=cand)
    return {"ok": True, "id": aid, "product_id": pid, "match_status": status}


@app.post("/api/answers/update")
def api_answer_update(payload: dict = Body(default={})):
    aid = int(payload.pop("id"))
    fields = {k: payload[k] for k in payload}
    # Gán SP tay -> confirmed (rời cảnh báo); gỡ SP -> 'unmatched' (hiện cảnh báo lại). Giống video giới thiệu.
    if "product_id" in fields:
        fields.setdefault("match_status", "confirmed" if fields["product_id"] else "unmatched")
    db.update_answer_video(aid, **fields)
    return {"ok": True}


@app.post("/api/answers/delete")
def api_answer_delete(payload: dict = Body(default={})):
    a = db.get_answer_video(int(payload["id"]))
    db.delete_answer_video(int(payload["id"]))
    if a and a.file_path:
        db.delete_videos_by_path(a.file_path)    # xóa khỏi Kịch bản AI -> mất luôn ở thư viện video (cùng file)
    return {"ok": True}


@app.post("/api/answer_by_intent")
def api_answer_by_intent(payload: dict = Body(default={})):
    """Chọn video trả lời (round-robin) của 1 intent rồi đẩy vào hàng đợi theo mode."""
    intent_id = int(payload.get("intent_id", 0))
    av = db.pick_answer_for_intent(intent_id)
    if not av:
        return {"ok": False, "error": "Intent chưa có video trả lời (đang bật)"}
    intent = db.get_intent(intent_id)
    mode = payload.get("mode") or (intent.trigger_mode if intent else "enqueue")
    ok = controller.enqueue_answer(av.id, mode)
    return {"ok": bool(ok), "video": av.name or av.file_path,
            "error": None if ok else "Trùng video liền trước trong hàng đợi"}


@app.post("/api/answers/play")
def api_answer_play(payload: dict = Body(default={})):
    """Phát thử/đẩy 1 video trả lời vào hàng đợi (mode theo intent nếu không truyền)."""
    av = db.get_answer_video(int(payload["id"]))
    if not av:
        return JSONResponse({"ok": False, "error": "không thấy answer video"}, status_code=400)
    mode = payload.get("mode")
    if not mode:
        intent = db.get_intent(av.intent_id) if av.intent_id else None
        mode = intent.trigger_mode if intent else "enqueue"
    ok = controller.enqueue_answer(av.id, mode)
    return {"ok": bool(ok), "error": None if ok else "Trùng video liền trước trong hàng đợi"}


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            with _cache_lock:
                snapshot = dict(_status_cache)
            await websocket.send_json(snapshot)   # chỉ đọc cache, không gọi OBS -> không chặn event loop
            await asyncio.sleep(1.3)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
