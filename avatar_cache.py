"""Shared MuseTalk avatar-cache inspection/cleanup logic (engines/MuseTalk/results/v15/avatars_realtime).

This covers only the REALTIME streaming cache (musetalk_stream_api.py) — its
avatar_id is a content hash of the source video, so a rebuilt/overwritten
source produces a new id and leaves the old one dangling (STALE) until swept
here. The batch video-render cache (musetalk_render_server.py, results/v15/
avatars_video) doesn't need this: it expires on a fixed 3-day timer instead,
see queue_worker.py's cleanup_old_musetalk_avatar_cache().

Both web_ui.py's "🗄️ Avatar Cache" tab and the FastAPI backend need the same
LIVE/STALE/TEST classification — see list_entries()'s docstring for what that
means. Structured as plain dicts here; web_ui.py formats them into Gradio
Dataframe rows, the API serializes them straight to JSON.
"""
import hashlib
import json
import subprocess
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AVATAR_CACHE_ROOT = ROOT / "engines" / "MuseTalk" / "results" / "v15" / "avatars_realtime"


def hash_video(path) -> str:
    """SHA256 nội dung file — cùng thuật toán với musetalk_adapter._video_cache_key /
    musetalk_stream_api.video_fingerprint, để so khớp avatar cache với video nguồn hiện tại."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return f"avatar-{h.hexdigest()}"


def video_duration(path):
    """Thời lượng video (giây) qua ffprobe, hoặc None nếu không đọc được."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float(out.stdout.strip())
    except Exception:
        return None


def source_name(video_path):
    """Tên file gốc trước khi convert/ghi đè lên video_path (xem stream_ui._normalize_facebook_avatar).

    Sidecar chỉ được ghi CÙNG LÚC với lần convert gần nhất -> chỉ đúng cho video ĐANG khớp
    hash hiện tại (LIVE); với bản STALE thì tên gốc đã mất vĩnh viễn cùng lúc video bị ghi đè,
    không có cách nào phục hồi.
    """
    sidecar = Path(video_path).with_suffix(".source.txt")
    try:
        return sidecar.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def list_entries() -> list[dict]:
    """Liệt kê avatar cache MuseTalk trên đĩa, gom theo video nguồn.

    Mỗi avatar_id là hash nội dung video lúc build. Nếu video nguồn bị ghi đè sau đó,
    hash mới không còn khớp -> cache cũ (STALE) không bao giờ được server tái sử dụng nữa,
    nhưng cũng không tự xóa -> chiếm đĩa vô ích. Hash mỗi video CHỈ 1 lần dù nhiều avatar
    cùng trỏ tới nó.

    Each dict: {video_name, source_name, duration_secs, avatar_id, size_bytes,
                built_at (ISO string), status ("LIVE"|"STALE"|"TEST"|"MISSING")}.
    """
    if not AVATAR_CACHE_ROOT.is_dir():
        return []
    entries = []
    for d in sorted(AVATAR_CACHE_ROOT.iterdir()):
        info_path = d / "avator_info.json"
        if not info_path.is_file():
            continue
        try:
            info = json.loads(info_path.read_text())
        except Exception:
            continue
        size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        entries.append({
            "avatar_id": d.name,
            "video_path": info.get("video_path") or "?",
            "size_bytes": size,
            "ctime": info_path.stat().st_mtime,
        })

    hash_cache, duration_cache = {}, {}

    def _live_hash(video_path):
        if video_path not in hash_cache:
            p = Path(video_path)
            hash_cache[video_path] = hash_video(p) if p.is_file() else None
        return hash_cache[video_path]

    def _duration_of(video_path):
        if video_path not in duration_cache:
            p = Path(video_path)
            duration_cache[video_path] = video_duration(p) if p.is_file() else None
        return duration_cache[video_path]

    out = []
    for e in sorted(entries, key=lambda x: (x["video_path"], -x["ctime"])):
        aid = e["avatar_id"]
        is_live = False
        if aid.startswith("_test"):
            status = "TEST"
        else:
            lh = _live_hash(e["video_path"])
            if lh is None:
                status = "MISSING"
            elif lh.startswith(aid):
                status = "LIVE"
                is_live = True
            else:
                status = "STALE"
        out.append({
            "video_name": Path(e["video_path"]).name,
            "source_name": source_name(e["video_path"]) if is_live else None,
            "duration_secs": _duration_of(e["video_path"]),
            "avatar_id": aid,
            "size_bytes": e["size_bytes"],
            "built_at": datetime.fromtimestamp(e["ctime"]).isoformat(),
            "status": status,
        })
    return out


def clear_stale() -> tuple[list[str], int]:
    """Delete every STALE avatar cache dir. Returns (removed_avatar_ids, freed_bytes)."""
    removed, freed = [], 0
    for entry in list_entries():
        if entry["status"] == "STALE":
            import shutil
            shutil.rmtree(AVATAR_CACHE_ROOT / entry["avatar_id"], ignore_errors=True)
            removed.append(entry["avatar_id"])
            freed += entry["size_bytes"]
    return removed, freed


def delete_entry(avatar_id: str) -> int:
    """Delete ONE avatar cache dir by id. Returns freed_bytes.

    Matches against list_entries() (real dirs under AVATAR_CACHE_ROOT) rather than
    joining avatar_id onto the path directly, so an unknown/crafted id can't escape
    AVATAR_CACHE_ROOT.
    """
    for entry in list_entries():
        if entry["avatar_id"] == avatar_id:
            import shutil
            shutil.rmtree(AVATAR_CACHE_ROOT / avatar_id, ignore_errors=True)
            return entry["size_bytes"]
    raise FileNotFoundError(f"Avatar cache '{avatar_id}' không tồn tại.")
