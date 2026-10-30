"""Cầu nối máy render vào hàng đợi akali — xem doc_api/APi_RENDER_QUEUE.md.

Vòng lặp (mục 5 của tài liệu): rảnh? -> POST /claim -> tải folder Drive -> nhận
diện đúng 1 Excel + 1 video -> process_excel -> đè voice akali chỉ định ->
submit_jobs -> heartbeat tới khi mọi dòng xong (TTS lẫn render, kể cả upload
Drive) -> export_results -> upload Excel kết quả -> POST /complete (hoặc /fail
nếu không dòng nào ra video). Idempotency qua bảng akali_batches (mục 6): bridge
chết giữa chừng rồi bật lại thì nối tiếp đúng batch cũ thay vì dựng batch mới.

CHỈ ĐỌC hàng đợi render qua process_excel/submit_jobs — không tự render.
queue_worker.py (chạy riêng, 24/7) mới là thứ thật sự lấy job ra render.

Run with: conda activate latentsync && python akali_bridge.py
"""
import fcntl
import os
import shutil
import signal
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

import dashboard_reporter
import database as db
import excel_import as xi
import google_drive_upload as gdrive
import gpu_info
import tts_db
from env_path import resolve_env_path

ROOT = Path(__file__).parent
ENV_PATH = resolve_env_path(ROOT / ".env")
_env_mtime = None

WORKER_LOCK_PATH = ROOT / ".akali_bridge.lock"

# process_excel() bắt buộc 2 cột này không trống (mục 4 của tài liệu) dù giá trị
# thật bị đè lại ở bước 8 — akali không điền voice per-row nên bridge phải tự
# điền placeholder trước khi gọi process_excel, nếu không dòng đó bị văng vào
# `errors` ngay tại bước đọc Excel, không bao giờ tới bước đè được.
_PLACEHOLDER_PROVIDER = "autovoice"
_PLACEHOLDER_VOICE = "__akali_placeholder__"

_VIDEO_EXTS = (".mp4", ".mov", ".mkv", ".webm", ".avi")
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")

# Fun-5B (ảnh nhân vật + video mẫu, không Excel/TTS) — doc_api/AKALI_FUN5B.md.
FUN5B_ENGINE = "fun5b"
_FUN5B_BATCH_PREFIX = "fun5b:"  # akali_batches.batch_id = "fun5b:<render job id>"
_FUN5B_UPLOAD_RETRY_DELAYS = (60, 180, 600)  # giây chờ trước mỗi lần thử upload lại (§5.4)
_FUN5B_UPLOADING_PROGRESS = 97  # render xong, đang đẩy lên Drive

_RUNNING = True
_LOCK_FILE = None


def _log(msg):
    print(f"[akali {datetime.now():%H:%M:%S}] {msg}", flush=True)


class BridgeFailure(Exception):
    """Lỗi đủ rõ ràng để quyết định retryable ngay tại chỗ raise (mục 3.4)."""

    def __init__(self, message, retryable):
        super().__init__(message)
        self.retryable = retryable


# ---------------------------------------------------------------- cấu hình (.env, mục 8)

def _reload_env_if_changed():
    global _env_mtime
    try:
        mtime = ENV_PATH.stat().st_mtime
    except OSError:
        return
    if mtime != _env_mtime:
        load_dotenv(ENV_PATH, override=True)
        _env_mtime = mtime


def _env(name, default=""):
    _reload_env_if_changed()
    return os.environ.get(name, default).strip()


def _base_url():
    return _env("AKALI_URL").rstrip("/")


def _worker_key():
    return _env("AKALI_WORKER_KEY")


def _worker_id():
    """Dùng chung tên máy với tab Dashboard Fleet (NODE_NAME/hostname) thay vì
    một biến .env riêng — một máy chỉ cần đặt tên một chỗ."""
    return dashboard_reporter.node_name()


def _poll_seconds():
    return float(_env("AKALI_POLL_SECONDS", "10") or 10)


def _heartbeat_seconds():
    return float(_env("AKALI_HEARTBEAT_SECONDS", "60") or 60)


def _reconcile_seconds():
    """Nhịp đối soát với akali lúc rảnh (mục 3.6 khuyến nghị 5-10 phút)."""
    return float(_env("AKALI_RECONCILE_SECONDS", "300") or 300)


def _workdir():
    d = Path(_env("AKALI_WORKDIR") or "/var/tmp/akali_batches")
    d.mkdir(parents=True, exist_ok=True)
    return d


def bridge_enabled():
    return bool(_base_url()) and bool(_worker_key())


def status():
    return {
        "configured": bridge_enabled(),
        "akali_url": _env("AKALI_URL") or None,
        "worker_key": _worker_key() or None,
        "worker_id": _worker_id(),  # luôn có giá trị — tên máy hoặc hostname, xem _worker_id()
    }


def _update_env_file(updates):
    """Ghi/đè KEY=VALUE vào .env, giữ nguyên mọi dòng khác — cùng cách làm với
    dashboard_reporter._update_env_file / google_drive_upload._update_env_file
    (mỗi module tự có bản riêng, không dùng chung — quy ước sẵn có của repo)."""
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    seen = set()
    for i, line in enumerate(lines):
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key = line.split("=", 1)[0].strip()
        if key in updates:
            lines[i] = f"{key}={updates[key]}"
            seen.add(key)
    missing = [k for k in updates if k not in seen]
    if missing:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(f"{k}={updates[k]}" for k in missing)
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    ENV_PATH.write_text("\n".join(lines) + "\n")
    os.chmod(ENV_PATH, 0o600)  # AKALI_WORKER_KEY là bí mật
    for k, v in updates.items():
        os.environ[k] = v
    global _env_mtime
    _env_mtime = ENV_PATH.stat().st_mtime  # vừa tự ghi — đừng tự reload lại ngay


def save_settings(akali_url, worker_key):
    """UI (tab kết nối akali) gọi hàm này thay vì bắt sửa tay .env — cùng trust
    model với dashboard_reporter.save_settings(): form round-trip lại secret hiện
    tại, nên xoá trắng field nghĩa là XOÁ THẬT, không có sentinel 'giữ nguyên'.

    Không có worker_id ở đây — nó ăn theo tên máy của tab Dashboard Fleet
    (xem _worker_id()), không phải cấu hình riêng của tab này.

    KHÔNG có bước "kiểm tra kết nối" thật ở đây (khác dashboard_reporter.test_push()):
    4 endpoint akali_bridge dùng đều có tác dụng phụ thật, đặc biệt claim() lấy
    luôn 1 job thật ra khỏi hàng đợi nếu đang có — gọi thử claim() chỉ để test
    kết nối có thể vô tình chiếm 1 job thật của máy khác. akali không có endpoint
    health/ping riêng (mục 3 của doc_api/APi_RENDER_QUEUE.md), nên chỉ lưu +
    báo lại trạng thái "đã điền đủ", không giả vờ đã xác nhận kết nối thật.
    """
    _update_env_file({
        "AKALI_URL": (akali_url or "").strip(),
        "AKALI_WORKER_KEY": (worker_key or "").strip(),
    })


# ---------------------------------------------------------------- hợp đồng API akali (mục 3)

def _headers():
    return {"Authorization": f"Bearer {_worker_key()}", "Content-Type": "application/json"}


def _post(path, payload, timeout=15):
    r = requests.post(_base_url() + path, headers=_headers(), json=payload, timeout=timeout)
    r.raise_for_status()
    return r


def _claim_engines():
    """Engine máy này chạy được NGAY LÚC NÀY, gửi kèm mỗi lần claim (doc_api/AKALI_FUN5B.md §4).

    musetalk luôn có; latentsync theo đúng điều kiện của UI chọn engine (gpu_info); fun5b chỉ khi
    đã tự cài xong (và không đang cài dở) và GPU đủ VRAM. Tính lại mỗi lần vì máy có thể vừa tự
    cài xong Fun-5B giữa chừng. Kiểm tra lỗi thì bỏ engine đó đi, không bao giờ làm hỏng claim."""
    engines = ["musetalk"]
    try:
        if not gpu_info.latentsync_blocked():
            engines.append("latentsync")
    except Exception as e:
        _log(f"không kiểm tra được latentsync, bỏ khỏi engines: {e}")
    try:
        import fun5b_adapter
        import fun5b_setup

        if fun5b_adapter.unavailable_reason()[0] is None and not fun5b_setup.running():
            engines.append(FUN5B_ENGINE)
    except Exception as e:
        _log(f"không kiểm tra được fun5b, bỏ khỏi engines: {e}")
    return engines


def claim():
    """None nếu chưa có việc (204). Raise nếu akali lỗi thật (4xx/5xx khác 204).
    akali bản cũ bỏ qua `engines` (chỉ đọc worker_id) nên gửi kèm luôn là an toàn."""
    r = requests.post(_base_url() + "/api/worker/claim", headers=_headers(),
                       json={"worker_id": _worker_id(), "engines": _claim_engines()}, timeout=15)
    if r.status_code == 204:
        return None
    r.raise_for_status()
    return r.json()


def _conflict_reason(e):
    """Lấy `reason` trong body 409 (mục 3.2): canceled | lease_lost | not_found.

    Trả None nếu không phải 409. Body hỏng/thiếu reason -> 'unknown', vẫn là 409
    nên vẫn phải dừng — chỉ là không biết vì sao, xử như mất lease cho an toàn.
    """
    if e.response is None or e.response.status_code != 409:
        return None
    try:
        return (e.response.json() or {}).get("reason") or "unknown"
    except ValueError:
        return "unknown"


def heartbeat(job_id, claim_token, progress, output_url=None):
    """None nếu còn giữ lease. Khác None = 409, trả luôn `reason` để caller xử đúng
    nhánh: 'canceled' phải dọn hàng chờ nội bộ (mục 4.1), 'lease_lost'/'not_found'
    thì chỉ cần buông. 409 KHÔNG phải lỗi tạm thời — heartbeat lại vẫn 409 mãi.

    output_url (tuỳ chọn): link thư mục Drive của batch, gửi sớm để dashboard hiện
    nút "Mở" khi job còn đang chạy (docs/early-output-url.md). Không đổi status."""
    # Backend không kẹp giá trị (mục 11) — kẹp ở đây để dashboard không vẽ
    # thanh tiến độ quá 100%.
    payload = {"claim_token": claim_token, "progress": max(0, min(100, int(progress)))}
    if output_url:
        payload["output_url"] = output_url
    try:
        _post(f"/api/worker/jobs/{job_id}/heartbeat", payload)
        return None
    except requests.HTTPError as e:
        reason = _conflict_reason(e)
        if reason:
            return reason
        raise


def cancel(job_id, claim_token, reason):
    """Báo job bị huỷ Ở PHÍA MÁY RENDER (mục 3.5). KHÔNG dùng /fail cho việc này:
    /fail làm job hiện ra là Lỗi và VẪN được giao lại cho máy khác theo
    max_attempts — tức lệnh huỷ của người dùng bị lật ngược."""
    _post(f"/api/worker/jobs/{job_id}/cancel",
          {"claim_token": claim_token, "reason": str(reason)[:500]})


def list_my_jobs():
    """Những job akali cho rằng máy này đang giữ (mục 3.6) — dùng để đối soát.
    Không cần bridge nhớ gì, chỉ cần worker_id của chính mình."""
    r = requests.get(_base_url() + "/api/worker/jobs", headers=_headers(),
                     params={"worker_id": _worker_id()}, timeout=15)
    r.raise_for_status()
    return (r.json() or {}).get("jobs", [])


def complete(job_id, claim_token, output_url):
    _post(f"/api/worker/jobs/{job_id}/complete",
          {"claim_token": claim_token, "output_url": output_url})


def fail(job_id, claim_token, error, retryable):
    _post(f"/api/worker/jobs/{job_id}/fail",
          {"claim_token": claim_token, "error": str(error)[:2000], "retryable": bool(retryable)})


# ---------------------------------------------------------------- idempotency (mục 6)

def _connect():
    con = sqlite3.connect(db.DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    return con


def _init_batches_table():
    with _connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS akali_batches (
                akali_job_id  INTEGER PRIMARY KEY,
                claim_token   TEXT NOT NULL,
                batch_id      TEXT NOT NULL,
                excel_stem    TEXT NOT NULL,
                status        TEXT NOT NULL DEFAULT 'running',
                created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )
            """
        )
        # Cờ huỷ Ở CẤP BATCH, thêm sau nên phải ALTER cho DB cũ (cùng cách
        # database.init_db() migrate cột render config). Thiếu cờ này thì bridge
        # phải ĐOÁN ý định người dùng từ trạng thái từng dòng, mà đoán thì không
        # phân biệt được "huỷ 1 video" với "huỷ cả batch" — xem _wait_for_batch().
        existing = {r["name"] for r in con.execute("PRAGMA table_info(akali_batches)")}
        if "cancel_requested" not in existing:
            con.execute("ALTER TABLE akali_batches ADD COLUMN "
                        "cancel_requested INTEGER NOT NULL DEFAULT 0")
        # Link thư mục Drive đã gửi sớm qua heartbeat — lưu bền để bridge khởi động
        # lại giữa chừng không phải gọi Drive API dựng lại link.
        if "output_url" not in existing:
            con.execute("ALTER TABLE akali_batches ADD COLUMN output_url TEXT")


def request_batch_cancel(akali_job_id=None):
    """Đánh dấu huỷ Ở CẤP BATCH. akali_job_id=None -> mọi batch đang chạy.

    Trả list (akali_job_id, excel_stem) đã đánh dấu, để caller dừng luôn render
    job của chúng thay vì đợi bridge phát hiện ở vòng poll kế tiếp.

    Chỉ ĐẶT CỜ, không tự gọi /cancel: chỉ bridge mới giữ claim_token hợp lệ, và
    akali bắt buộc có token đúng mới cho huỷ (mục 3.5 — không có fencing thì một
    máy đi lạc huỷ được job của máy khác).
    """
    with _connect() as con:
        con.execute("BEGIN IMMEDIATE")
        q = "SELECT akali_job_id, excel_stem FROM akali_batches WHERE status = 'running'"
        args = ()
        if akali_job_id is not None:
            q += " AND akali_job_id = ?"
            args = (akali_job_id,)
        rows = con.execute(q, args).fetchall()
        for row in rows:
            con.execute("UPDATE akali_batches SET cancel_requested = 1 WHERE akali_job_id = ?",
                        (row["akali_job_id"],))
        return [(row["akali_job_id"], row["excel_stem"]) for row in rows]


def _batch_cancel_requested(akali_job_id):
    with _connect() as con:
        row = con.execute(
            "SELECT cancel_requested FROM akali_batches WHERE akali_job_id = ?", (akali_job_id,)
        ).fetchone()
        return bool(row and row["cancel_requested"])


def _get_batch(akali_job_id):
    with _connect() as con:
        row = con.execute(
            "SELECT * FROM akali_batches WHERE akali_job_id = ?", (akali_job_id,)
        ).fetchone()
        return dict(row) if row else None


def _save_batch(akali_job_id, claim_token, batch_id, excel_stem):
    """upsert, không phải INSERT thuần: akali có thể giao LẠI cùng akali_job_id sau
    khi lần trước đã 'reported'/'abandoned' (mục 7.4 — akali retry cả batch), lúc đó
    đây là một lượt chạy mới với batch_id mới nhưng cùng khoá chính — INSERT thuần
    sẽ vi phạm PRIMARY KEY."""
    with _connect() as con:
        con.execute(
            "INSERT INTO akali_batches (akali_job_id, claim_token, batch_id, excel_stem) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(akali_job_id) DO UPDATE SET "
            "claim_token = excluded.claim_token, batch_id = excluded.batch_id, "
            "excel_stem = excluded.excel_stem, status = 'running', "
            # Phải xoá cờ huỷ của LƯỢT TRƯỚC: không xoá thì lượt chạy mới
            # (akali giao lại cùng job id) bị huỷ oan ngay ở vòng lặp đầu tiên.
            # Link sớm cũng xoá: lượt mới chỉ gửi link khi chính nó đã có video lên Drive.
            "cancel_requested = 0, output_url = NULL",
            (akali_job_id, claim_token, batch_id, excel_stem),
        )


def _mark_batch(akali_job_id, status):
    with _connect() as con:
        con.execute(
            "UPDATE akali_batches SET status = ? WHERE akali_job_id = ?", (status, akali_job_id)
        )


def _batch_output_url(akali_job_id):
    with _connect() as con:
        row = con.execute(
            "SELECT output_url FROM akali_batches WHERE akali_job_id = ?", (akali_job_id,)
        ).fetchone()
        return row["output_url"] if row else None


def _set_batch_output_url(akali_job_id, output_url):
    with _connect() as con:
        con.execute(
            "UPDATE akali_batches SET output_url = ? WHERE akali_job_id = ?",
            (output_url, akali_job_id),
        )


# ---------------------------------------------------------------- tải + nhận diện file (mục 4, 5 bước 4-6)

def _download_batch_folder(akali_job_id, drive_file_id):
    dest = _workdir() / str(akali_job_id)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        gdrive.download_folder(drive_file_id, dest)
    except Exception as e:
        raise BridgeFailure(f"tải folder Drive lỗi: {e}", retryable=True) from e
    return dest


def _identify_files(folder):
    """Đúng 1 .xlsx + 1 video (mục 4). Raise BridgeFailure(retryable=False) nếu sai."""
    xlsx = [p for p in folder.iterdir() if p.suffix.lower() == ".xlsx"]
    videos = [p for p in folder.iterdir() if p.suffix.lower() in _VIDEO_EXTS]
    if len(xlsx) != 1:
        raise BridgeFailure(f"cần đúng 1 file .xlsx trong folder, thấy {len(xlsx)}", retryable=False)
    if len(videos) != 1:
        raise BridgeFailure(f"cần đúng 1 file video trong folder, thấy {len(videos)}", retryable=False)
    return xlsx[0], videos[0]


def _fill_voice_placeholders(xlsx_path):
    """Điền placeholder (khác rỗng, giá trị thật không quan trọng — bị đè ở bước 8)
    vào 2 cột tts_provider/tts_voice nếu Excel akali gửi để trống, để process_excel
    không văng dòng đó vào `errors` trước khi bridge kịp đè (xem mục 4)."""
    df = pd.read_excel(xlsx_path)
    changed = False
    for col, placeholder in (("tts_provider", _PLACEHOLDER_PROVIDER), ("tts_voice", _PLACEHOLDER_VOICE)):
        if col not in df.columns:
            df[col] = placeholder
            changed = True
            continue
        blank = df[col].isna() | (df[col].astype(str).str.strip() == "")
        if blank.any():
            # Cột toàn NaN (akali không gửi voice per-row) suy ra dtype float64 —
            # gán thẳng chuỗi vào đó là incompatible-dtype (pandas cảnh báo, tương
            # lai sẽ raise). Ép về object trước để gán chuỗi luôn hợp lệ.
            df[col] = df[col].astype(object)
            df.loc[blank, col] = placeholder
            changed = True
    if changed:
        df.to_excel(xlsx_path, index=False)


# ---------------------------------------------------------------- chuẩn bị + nộp batch (mục 5 bước 6-9)

def _prepare_and_submit(akali_job_id, folder, params):
    xlsx_path, video_path = _identify_files(folder)

    # Bước 6 — đổi tên Excel để khớp thư mục Drive theo excel_stem (mục 2.2), và để
    # 2 batch trùng tên file akali gửi không đổ chung 1 thư mục.
    excel_stem = f"{xlsx_path.stem}__job{akali_job_id}"
    renamed = xlsx_path.with_name(f"{excel_stem}.xlsx")
    xlsx_path.rename(renamed)

    _fill_voice_placeholders(renamed)

    # Bước 7
    ready_rows, errors = xi.process_excel(
        str(renamed),
        default_videos=[{"name": video_path.stem, "path": str(video_path)}],
    )
    if not ready_rows:
        raise BridgeFailure(
            f"process_excel không có dòng hợp lệ nào ({len(errors)} lỗi): {errors[:3]}",
            retryable=False,
        )

    # Bước 8 — đè voice: mọi dòng dùng đúng voice akali chỉ định, bất kể Excel ghi gì.
    for r in ready_rows:
        r["tts_provider"] = params.get("tts_provider")
        r["tts_voice"] = params.get("voice_id")

    # Bước 9 — ánh xạ cấu hình render cho cả batch.
    render_config = {
        k: params.get(k) for k in
        ("model_res", "out_res", "guidance", "steps", "seed",
         "enhance_mouth", "enhance_region", "input_type", "engine")
        if params.get(k) is not None
    }

    batch_id = uuid.uuid4().hex[:12]
    try:
        _enqueued, _warnings, batch_id, _skipped = xi.submit_jobs(
            ready_rows, batch_id=batch_id, excel_path=str(renamed), render_config=render_config,
        )
    except Exception as e:
        raise BridgeFailure(f"submit_jobs lỗi: {e}", retryable=True) from e

    return batch_id, excel_stem


# ---------------------------------------------------------------- Fun-5B (doc_api/AKALI_FUN5B.md)
#
# Một job akali = 1 ảnh nhân vật + 1 video mẫu = 1 render job (engine fun5b), không Excel/TTS.
# Kết quả đẩy vào folder Drive riêng "fun5b__job<id>" chỉ chứa đúng video thành phẩm: akali trừ
# point theo tổng độ dài video trong folder output_url (§3.3).

def _fun5b_folder_name(akali_job_id):
    return f"fun5b__job{akali_job_id}"


def _identify_fun5b_files(folder):
    """(ảnh nhân vật, video mẫu, ảnh nền | None), nhận diện theo đuôi file (§2): akali đặt tên
    nhan_vat.* / video_mau.* / background.*, nhưng folder người dùng tự dựng cũng phải chạy được.
    File khác (vd .xlsx) bị bỏ qua. Raise BridgeFailure(retryable=False) nếu sai số lượng."""
    files = sorted(p for p in folder.iterdir() if p.is_file())
    images = [p for p in files if p.suffix.lower() in _IMAGE_EXTS]
    backgrounds = [p for p in images if p.stem.lower() == "background"]
    refs = [p for p in images if p not in backgrounds]
    videos = [p for p in files if p.suffix.lower() in _VIDEO_EXTS]
    if len(refs) != 1:
        raise BridgeFailure(f"cần đúng 1 ảnh nhân vật (.png/.jpg/.webp, không tính background.*), thấy {len(refs)}",
                            retryable=False)
    if len(videos) != 1:
        raise BridgeFailure(f"cần đúng 1 video mẫu trong folder, thấy {len(videos)}", retryable=False)
    if len(backgrounds) > 1:
        raise BridgeFailure(f"chỉ được 1 ảnh background.*, thấy {len(backgrounds)}", retryable=False)
    return refs[0], videos[0], (backgrounds[0] if backgrounds else None)


def _flag(params, key, default):
    value = params.get(key)
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _fun5b_options(params):
    """params akali -> kwargs của db.add_fun5b_job (§3.1). Thiếu khoá thì dùng mặc định; giá trị
    sai thì BridgeFailure(retryable=False) — job khác máy cũng sai y hệt."""
    preset = params.get("preset") or db.FUN5B_DEFAULT_PRESET
    if preset not in db.FUN5B_PRESETS:
        raise BridgeFailure(f"preset không hợp lệ: {preset!r} (dùng {', '.join(db.FUN5B_PRESETS)})", retryable=False)
    seed = params.get("seed")
    try:
        seed = db.FUN5B_DEFAULT_SEED if seed in (None, "") else int(seed)
    except (TypeError, ValueError):
        raise BridgeFailure(f"seed không hợp lệ: {seed!r}", retryable=False) from None
    table = params.get("table_line")
    try:
        table = None if table in (None, "") or float(table) == 0 else float(table)
    except (TypeError, ValueError):
        raise BridgeFailure(f"table_line không hợp lệ: {table!r}", retryable=False) from None
    if table is not None and not 1 <= table <= 99:
        raise BridgeFailure(f"table_line phải trong 1–99 (% chiều cao) hoặc null, nhận {table!r}", retryable=False)
    return {
        "preset": preset, "seed": seed,
        "keep_background": _flag(params, "keep_background", True),
        "auto_background": _flag(params, "auto_background", True),
        "table_line": None if table is None else table / 100,
        "sharp_hands": _flag(params, "sharp_hands", False),
    }


def _result_folder_id(folder_name):
    return gdrive.ensure_folder(gdrive.get_service(), folder_name, gdrive.default_folder_id())


def _prepare_and_submit_fun5b(akali_job_id, folder, params):
    """F1–F5: nhận diện file, kiểm params, dọn folder kết quả, đưa 1 render job vào hàng đợi.
    Trả (batch_id, tên folder kết quả) để lưu vào akali_batches như batch Excel."""
    image, video, background = _identify_fun5b_files(folder)
    options = _fun5b_options(params)
    if not gdrive.drive_enabled():
        # Không có Drive thì không có output_url -> không bao giờ /complete được. Để máy khác làm.
        raise BridgeFailure("máy render chưa bật upload Google Drive", retryable=True)

    folder_name = _fun5b_folder_name(akali_job_id)
    # F3 — "Chạy lại" trên akali giữ nguyên akali_job_id, nên folder có thể còn video của lần
    # trước (máy này hoặc máy khác cùng tài khoản Drive). Để lại thì akali tính point 2 lần.
    try:
        trashed = gdrive.trash_folder_contents(_result_folder_id(folder_name))
    except Exception as e:
        raise BridgeFailure(f"không dọn được folder kết quả {folder_name} trên Drive: {e}", retryable=True) from e
    if trashed:
        _log(f"job #{akali_job_id}: folder {folder_name} còn {len(trashed)} file cũ — đã chuyển vào thùng rác")

    try:
        render_job_id = db.add_fun5b_job(f"fun5b_job{akali_job_id}", image, video, background_path=background,
                                         drive_folder=folder_name, **options)
    except ValueError as e:
        raise BridgeFailure(f"tham số Fun-5B không hợp lệ: {e}", retryable=False) from e
    _log(f"job #{akali_job_id}: Fun-5B render job #{render_job_id} ({image.name} + {video.name}, "
         f"preset={options['preset']}) -> Drive/{folder_name}")
    return f"{_FUN5B_BATCH_PREFIX}{render_job_id}", folder_name


def _fun5b_progress(row):
    """progress 0-100 cho heartbeat. jobs.progress của Fun-5B đã gồm cả giai đoạn nạp model / vẽ
    nền / tách pose (render_job.FUN5B_STAGE_PROGRESS), không đứng yên ở 0 suốt ~10 phút đầu."""
    if row["status"] == db.STATUS_DONE:
        return _FUN5B_UPLOADING_PROGRESS
    if row["status"] == db.STATUS_RENDERING:
        return int((row.get("progress") or 0) * 100)
    return 0


def _is_drive_quota_error(error):
    text = str(error or "").lower()
    return "storagequotaexceeded" in text or "storage quota has been exceeded" in text


def _retry_fun5b_upload(row):
    """Upload lại video đã render (queue_worker chỉ thử 1 lần), cùng tên file và folder."""
    from queue_worker import _drive_safe_name

    path = row.get("output_path")
    if not path or not Path(path).is_file():
        raise RuntimeError(f"không còn file video để upload lại: {path}")
    info = gdrive.upload_file(path, name=_drive_safe_name(path), subfolder=row.get("drive_folder"))
    db.set_drive_result(row["id"], link=info.get("webViewLink"))


_FUN5B_UPLOAD_STUCK_SECONDS = 15 * 60  # done mà chưa có drive_link/drive_error: thread upload đã chết


def _wait_for_fun5b(akali_job_id, claim_token, render_job_id):
    """Heartbeat tới khi render job có kết quả cuối. Trả (outcome, row):
    done | failed | cancelled | cancel_requested | drive_quota | drive_failed | missing | stopped,
    hoặc reason của 409 ('canceled' | 'lease_lost' | 'not_found' | 'unknown')."""
    next_heartbeat = 0.0
    attempts, retry_at, done_since = 0, None, None
    while _RUNNING:
        row = db.get_job(render_job_id)
        if _batch_cancel_requested(akali_job_id):
            return "cancel_requested", row
        if row is None:
            return "missing", None
        now = time.monotonic()
        if now >= next_heartbeat:
            reason = heartbeat(akali_job_id, claim_token, _fun5b_progress(row))
            if reason:
                return reason, row
            next_heartbeat = now + _heartbeat_seconds()

        status = row["status"]
        if status in (db.STATUS_FAILED, db.STATUS_CANCELLED):
            return status, row
        if status == db.STATUS_DONE:
            if row.get("drive_link"):
                return "done", row
            error = row.get("drive_error")
            done_since = done_since or now
            if not error and now - done_since > _FUN5B_UPLOAD_STUCK_SECONDS:
                error = "upload Drive không kết thúc sau 15 phút"
                db.set_drive_result(render_job_id, error=error)
            if error:
                if _is_drive_quota_error(error):
                    return "drive_quota", row
                if attempts >= len(_FUN5B_UPLOAD_RETRY_DELAYS):
                    return "drive_failed", row
                if retry_at is None:
                    retry_at = now + _FUN5B_UPLOAD_RETRY_DELAYS[attempts]
                    _log(f"job #{akali_job_id}: upload Drive lỗi ({error}) — thử lại sau "
                         f"{_FUN5B_UPLOAD_RETRY_DELAYS[attempts]}s ({attempts + 1}/{len(_FUN5B_UPLOAD_RETRY_DELAYS)})")
                elif now >= retry_at:
                    attempts, retry_at = attempts + 1, None
                    try:
                        _retry_fun5b_upload(row)
                        _log(f"job #{akali_job_id}: upload lại lên Drive OK")
                    except Exception as e:
                        db.set_drive_result(render_job_id, error=e)
                        _log(f"job #{akali_job_id}: upload lại lỗi — {e}")
        time.sleep(min(5.0, _heartbeat_seconds()))
    return "stopped", None


def _finish_fun5b(akali_job_id, claim_token, batch_id, folder_name):
    """Bước 6–9 của nhánh F (§5.1) — dùng chung cho job mới và job nối lại lúc khởi động."""
    render_job_id = int(batch_id[len(_FUN5B_BATCH_PREFIX):])
    outcome, row = _wait_for_fun5b(akali_job_id, claim_token, render_job_id)

    if outcome == "canceled":  # huỷ trên dashboard akali (nhánh H)
        _abandon_batch(akali_job_id, batch_id, folder_name, "akali báo canceled")
        return
    if outcome in ("lease_lost", "not_found", "unknown"):
        _mark_batch(akali_job_id, "abandoned")
        _log(f"job #{akali_job_id}: 409 reason={outcome} — ngừng theo dõi, KHÔNG gọi /complete hay /fail")
        return
    if outcome == "stopped":
        _log(f"job #{akali_job_id}: bridge đang tắt — lần chạy sau nối tiếp qua akali_batches")
        return
    if outcome in ("cancel_requested", db.STATUS_CANCELLED):  # huỷ ở máy render (nhánh U)
        try:
            cancel(akali_job_id, claim_token, "người dùng dừng job ở máy render")
        except requests.HTTPError as e:
            _log(f"job #{akali_job_id}: /cancel lỗi — {e}")
        _abandon_batch(akali_job_id, batch_id, folder_name, "huỷ ở máy render")
        return

    failure = None
    if outcome == "missing":
        failure = (f"render job #{render_job_id} không còn trên máy render", True)
    elif outcome == db.STATUS_FAILED:
        error = row.get("error") or "render lỗi"
        # render_job.py in "[render_job] REJECTED: ..." khi đầu vào hỏng (không thấy người, file
        # hỏng): máy khác cũng hỏng y hệt. Lỗi khác (GPU, server chết) thì để máy khác thử.
        failure = (error, "REJECTED:" not in error)
    elif outcome == "drive_quota":
        failure = (f"Drive máy render hết dung lượng — dọn Drive rồi bấm Chạy lại ({row.get('drive_error')})", False)
    elif outcome == "drive_failed":
        failure = (f"upload Drive lỗi sau {len(_FUN5B_UPLOAD_RETRY_DELAYS)} lần thử lại: {row.get('drive_error')}. "
                   f"Video vẫn ở máy render: {row.get('output_path')}", True)
    if failure:
        _log(f"job #{akali_job_id}: /fail retryable={failure[1]} — {failure[0]}")
        fail(akali_job_id, claim_token, failure[0], retryable=failure[1])
        _mark_batch(akali_job_id, "abandoned")
        return

    # F6 — ngay trước /complete: folder chỉ được còn đúng video của render job này (một upload
    # dở của máy khác vừa mất lease có thể lên sau F3 — akali sẽ trừ point gấp đôi).
    try:
        keep = gdrive.file_id_from_link(row["drive_link"])
        if not keep:
            raise RuntimeError(f"không đọc được file id từ {row['drive_link']}")
        folder_id = _result_folder_id(folder_name)
        trashed = gdrive.trash_folder_contents(folder_id, keep_file_id=keep)
        if trashed:
            _log(f"job #{akali_job_id}: folder kết quả có thêm {len(trashed)} file lạ — đã chuyển vào thùng rác: {trashed}")
        output_url = f"https://drive.google.com/drive/folders/{folder_id}"
    except Exception as e:
        _log(f"job #{akali_job_id}: chưa dọn được folder kết quả (còn giữ lease, thử lại vòng sau) — {e}")
        return
    try:
        complete(akali_job_id, claim_token, output_url)
    except requests.HTTPError as e:
        _log(f"job #{akali_job_id}: /complete lỗi — {e} (job vẫn 'running', thử lại vòng sau)")
        return
    _mark_batch(akali_job_id, "reported")
    _log(f"job #{akali_job_id}: DONE -> {output_url}")


# ---------------------------------------------------------------- dọn batch bị huỷ (mục 4.1)

_ABANDON_RESWEEP_SECONDS = 15   # > thời gian tts_worker kẹt giữa add_job() và mark_done()


def _abandon_batch(akali_job_id, batch_id, excel_stem, reason):
    """6 bước ở mục 4.1 khi job bị huỷ: bỏ khỏi danh sách theo dõi, dừng render,
    xoá dòng chưa chạy khỏi tts_jobs, xoá workdir, đánh dấu abandoned, KHÔNG gọi
    /complete và KHÔNG gọi /fail.

    Đánh dấu 'abandoned' TRƯỚC khi dọn là có chủ đích: chết giữa chừng thì batch
    đã bị loại khỏi danh sách heartbeat vĩnh viễn (đúng chỗ chặn lỗi job 80 —
    heartbeat một job đã huỷ suốt 4 ngày), phần rác còn lại để vòng đối soát dọn
    nốt. Làm ngược lại thì crash giữa chừng là kẹt 'running' mãi mãi.
    """
    _mark_batch(akali_job_id, "abandoned")

    removed = tts_db.delete_unstarted_in_batch(batch_id)
    cancelled = db.request_cancel_by_drive_folder(excel_stem)
    _log(f"job #{akali_job_id}: huỷ ({reason}) — xoá {removed} dòng TTS chưa chạy, "
         f"huỷ {len(cancelled)} render job")

    # Quét lại: một dòng đang 'submitting' lúc nãy có thể vừa kịp tạo render job
    # SAU lượt quét đầu (tts_worker gọi add_job trước mark_done). Không quét lại
    # thì đúng job đó tiếp tục đốt GPU và đẩy video của batch đã huỷ lên Drive.
    deadline = time.monotonic() + _ABANDON_RESWEEP_SECONDS
    while _RUNNING and time.monotonic() < deadline:
        time.sleep(1.0)
    late = db.request_cancel_by_drive_folder(excel_stem)
    if late:
        _log(f"job #{akali_job_id}: quét lại bắt thêm {len(late)} render job sinh muộn")

    workdir = _workdir() / str(akali_job_id)
    shutil.rmtree(workdir, ignore_errors=True)


# ---------------------------------------------------------------- theo dõi tới khi xong (mục 5 bước 10-11)

def _row_finished(tts_status, render_status, job_row):
    """True nếu dòng này không còn gì để đợi nữa — thành công (đã có drive_link/
    drive_error), thất bại hẳn (TTS hết retry, render fail), hoặc đã bị huỷ.

    'cancelled' phải nằm ở đây: thiếu nó thì một dòng bị huỷ khiến finished < total
    vĩnh viễn, bridge treo trên batch đó cho tới khi mất lease."""
    if tts_status in ("failed_permanent", "failed_retryable"):
        return True
    if render_status in ("failed", "cancelled"):
        return True
    if render_status == "done":
        return bool(job_row and (job_row.get("drive_link") or job_row.get("drive_error")))
    return False


def _batch_snapshot(batch_id):
    """(total, finished, succeeded, cancelled, uploaded) — cancelled tách riêng để phân
    biệt 'batch hỏng' với 'batch bị người dùng dừng ở máy render' (nhánh U, mục 6).
    uploaded = số dòng đã THẬT SỰ lên Drive (có drive_link), không phải chỉ render
    xong: upload chạy ở thread nền sau mark_done."""
    results = tts_db.results_for_batch(batch_id)
    finished = succeeded = cancelled = uploaded = 0
    for r in results.values():
        job_row = db.get_job(r["render_job_id"]) if r.get("render_job_id") else None
        if job_row and job_row.get("drive_link"):
            uploaded += 1
        if _row_finished(r["tts_status"], r["render_status"], job_row):
            finished += 1
            if r["render_status"] == "done":
                succeeded += 1
            elif r["render_status"] == "cancelled":
                cancelled += 1
    return len(results), finished, succeeded, cancelled, uploaded


def _batch_folder_url(excel_stem):
    """Link thư mục batch, dựng ĐÚNG như bước 13 (_export_and_upload): cùng excel_stem
    + cùng thư mục gốc -> ensure_folder() trả cùng folder_id với chỗ queue_worker
    đang upload video vào."""
    service = gdrive.get_service()
    folder_id = gdrive.ensure_folder(service, excel_stem, gdrive.default_folder_id())
    return f"https://drive.google.com/drive/folders/{folder_id}"


def _early_output_url(akali_job_id, excel_stem, uploaded):
    """Link thư mục để gửi kèm heartbeat, hoặc None nếu chưa có video nào lên Drive.

    Lỗi Drive API KHÔNG được làm hỏng heartbeat: heartbeat trễ có thể làm hết lease
    và job bị giao cho máy khác. Lỗi thì trả None, nhịp sau thử lại."""
    url = _batch_output_url(akali_job_id)
    if url or not uploaded:
        return url
    try:
        url = _batch_folder_url(excel_stem)
    except Exception as e:
        _log(f"job #{akali_job_id}: chưa dựng được link thư mục Drive, thử lại nhịp sau — {e}")
        return None
    _set_batch_output_url(akali_job_id, url)
    _log(f"job #{akali_job_id}: đã có video trên Drive — gửi link thư mục sớm {url}")
    return url


def _wait_for_batch(akali_job_id, claim_token, batch_id, excel_stem):
    """Trả (outcome, succeeded, cancelled) — outcome: 'done' | 'stopped' | reason
    của 409 ('canceled' | 'lease_lost' | 'not_found' | 'unknown').

    progress là **phần trăm 0-100** (mục 11 của tài liệu đã chốt: backend ghi
    thẳng vào cột, dashboard vẽ `width: ${progress}%`).
    """
    next_heartbeat = 0.0
    while _RUNNING:
        total, finished, succeeded, cancelled, uploaded = _batch_snapshot(batch_id)
        # Nhánh U (mục 4.2) — có người bấm huỷ ở phía máy render. Kiểm cờ TRƯỚC
        # heartbeat để không báo tiến độ cho một batch vừa bị huỷ.
        if _batch_cancel_requested(akali_job_id):
            return "cancel_requested", succeeded, cancelled
        if time.monotonic() >= next_heartbeat:
            progress = int(finished * 100 / total) if total else 0
            output_url = _early_output_url(akali_job_id, excel_stem, uploaded)
            reason = heartbeat(akali_job_id, claim_token, progress, output_url)
            if reason:
                return reason, succeeded, cancelled
            next_heartbeat = time.monotonic() + _heartbeat_seconds()
        if total and finished >= total:
            return "done", succeeded, cancelled
        time.sleep(min(5.0, _heartbeat_seconds()))
    return "stopped", succeeded, cancelled


# ---------------------------------------------------------------- báo xong (mục 5 bước 12-13)

def _export_and_upload(batch_id, excel_stem):
    """export_results() rồi upload file kết quả vào ĐÚNG thư mục batch trên Drive
    (excel_stem — mục 2.2), trả link thư mục (mục 5 bước 13: upload_file() không
    trả folder id nên phải ensure_folder() riêng)."""
    out_path, _filled, _total = xi.export_results(batch_id)
    output_url = _batch_folder_url(excel_stem)
    gdrive.upload_file(out_path, folder_id=gdrive.default_folder_id(), subfolder=excel_stem)
    return output_url


# ---------------------------------------------------------------- xử lý 1 job akali (mục 5, toàn bộ)

def _finish_batch(akali_job_id, claim_token, batch_id, excel_stem):
    """Theo dõi tới khi xong (bước 10-11) rồi báo kết quả (bước 12-13) — dùng
    chung cho batch mới nộp lẫn batch cũ nối lại lúc khởi động (_resume_pending_batches)."""
    if batch_id.startswith(_FUN5B_BATCH_PREFIX):
        _finish_fun5b(akali_job_id, claim_token, batch_id, excel_stem)
        return
    outcome, succeeded, cancelled = _wait_for_batch(akali_job_id, claim_token, batch_id, excel_stem)

    if outcome == "canceled":
        # Nhánh H (mục 4.1) — người dùng bấm Huỷ trên dashboard.
        _abandon_batch(akali_job_id, batch_id, excel_stem, "akali báo canceled")
        return
    if outcome in ("lease_lost", "not_found", "unknown"):
        # Vẫn phải đánh dấu abandoned: để 'running' thì mọi lần khởi động sau đều
        # heartbeat lại một job không còn của mình (đúng lỗi job 80). Không dọn
        # hàng chờ nội bộ ở đây — 'lease_lost' nghĩa là máy khác đang làm batch
        # này, nhưng dòng tts_jobs là của RIÊNG máy này nên cứ để chạy nốt.
        _mark_batch(akali_job_id, "abandoned")
        _log(f"job #{akali_job_id}: 409 reason={outcome} — ngừng theo dõi vĩnh viễn, "
             f"KHÔNG gọi /complete hay /fail")
        return
    if outcome == "stopped":
        _log(f"job #{akali_job_id}: bridge đang tắt giữa chừng — để nguyên, lần chạy sau nối tiếp qua akali_batches")
        return

    if outcome == "cancel_requested" or (cancelled and succeeded == 0):
        # Nhánh U (mục 4.2) — huỷ ở phía máy render. Dùng /cancel chứ KHÔNG /fail,
        # vì /fail sẽ khiến akali giao lại đúng batch này cho máy khác chạy tiếp.
        #
        # Cờ cấp batch thắng số dòng đã xong: huỷ lúc đã render được 5/10 thì ý
        # người dùng vẫn là HUỶ, không phải "xong một nửa" — báo /complete lúc đó
        # sẽ hiện Xong trên dashboard, sai hẳn ý định. Điều kiện
        # `cancelled and succeeded == 0` giữ lại làm lưới đỡ cho batch bị huỷ hết
        # từng dòng qua UI cũ (không đi qua cờ).
        _log(f"job #{akali_job_id}: batch bị huỷ ở máy render "
             f"({succeeded} dòng đã xong, {cancelled} dòng bị huỷ) — /cancel")
        try:
            cancel(akali_job_id, claim_token, "người dùng dừng batch ở máy render")
        except requests.HTTPError as e:
            _log(f"job #{akali_job_id}: /cancel lỗi — {e}")
        _abandon_batch(akali_job_id, batch_id, excel_stem, "huỷ ở máy render")
        return

    if succeeded == 0:
        _log(f"job #{akali_job_id}: không dòng nào ra video — /fail (mục 10 giai đoạn 3)")
        fail(akali_job_id, claim_token, "Không có dòng nào render thành công", retryable=False)
        _mark_batch(akali_job_id, "abandoned")
        return

    try:
        output_url = _export_and_upload(batch_id, excel_stem)
    except Exception as e:
        _log(f"job #{akali_job_id}: lỗi export/upload kết quả (còn giữ lease, thử lại vòng sau) — {e}")
        return

    try:
        complete(akali_job_id, claim_token, output_url)
    except requests.HTTPError as e:
        _log(f"job #{akali_job_id}: /complete lỗi — {e} (batch vẫn 'running', thử lại vòng sau)")
        return
    _mark_batch(akali_job_id, "reported")
    _log(f"job #{akali_job_id}: DONE -> {output_url}")


def _handle_job(job):
    akali_job_id = job["id"]
    claim_token = job["claim_token"]
    params = job.get("params") or {}

    existing = _get_batch(akali_job_id)
    if existing and existing["status"] == "running":
        _log(f"job #{akali_job_id}: batch {existing['batch_id']} đã có (idempotency, mục 6) — theo dõi tiếp")
        batch_id, excel_stem = existing["batch_id"], existing["excel_stem"]
    else:
        try:
            folder = _download_batch_folder(akali_job_id, job.get("drive_file_id"))
            if params.get("engine") == FUN5B_ENGINE:
                batch_id, excel_stem = _prepare_and_submit_fun5b(akali_job_id, folder, params)
            else:
                batch_id, excel_stem = _prepare_and_submit(akali_job_id, folder, params)
        except BridgeFailure as e:
            _log(f"job #{akali_job_id}: {e} (retryable={e.retryable})")
            fail(akali_job_id, claim_token, str(e), retryable=e.retryable)
            return
        except Exception as e:
            _log(f"job #{akali_job_id}: lỗi không lường trước lúc chuẩn bị batch — {e}")
            fail(akali_job_id, claim_token, str(e), retryable=True)
            return
        _save_batch(akali_job_id, claim_token, batch_id, excel_stem)

    _finish_batch(akali_job_id, claim_token, batch_id, excel_stem)


def _reconcile():
    """Đối soát hai chiều với akali (mục 3.6 + 4.3) — lưới an toàn cho trường hợp
    heartbeat không cứu được: bridge mất trí nhớ (khởi động lại, tiến trình chết)
    hoặc akali đã sang tay job cho máy khác.

    Chạy lúc khởi động (TRƯỚC khi claim) và định kỳ lúc rảnh. Không chạy khi đang
    ôm batch — bridge đơn luồng nên chỉ cần đặt lời gọi ở vòng lặp chính là đủ;
    lúc đang chạy batch thì 409 của heartbeat đã lo phần việc tương đương.
    """
    remote = {j["id"]: j for j in list_my_jobs()}
    with _connect() as con:
        local = {r["akali_job_id"]: dict(r) for r in con.execute(
            "SELECT * FROM akali_batches WHERE status = 'running'"
        )}

    # akali nghĩ máy này đang giữ, nhưng máy không còn theo dõi -> trả lại để akali giao máy khác.
    for job_id in remote.keys() - local.keys():
        _log(f"đối soát: job #{job_id} akali giao cho máy này nhưng bridge không theo dõi — /fail retryable")
        try:
            fail(job_id, remote[job_id].get("claim_token"), "bridge mất dấu job", retryable=True)
        except requests.HTTPError as e:
            _log(f"đối soát: /fail job #{job_id} lỗi — {e}")

    # Máy còn theo dõi, akali thì không -> job đã bị huỷ hoặc sang tay. Dọn như bị huỷ.
    for job_id in local.keys() - remote.keys():
        row = local[job_id]
        _log(f"đối soát: job #{job_id} không còn ở akali — dọn hàng chờ nội bộ")
        _abandon_batch(job_id, row["batch_id"], row["excel_stem"], "không còn ở akali")


def _resume_pending_batches():
    """Lúc mới khởi động, chủ động nối lại MỌI batch còn 'running' từ lần chạy
    trước bằng claim_token đã lưu — KHÔNG chờ akali cấp lại đúng job đó qua
    /claim. Thiếu bước này thì một batch đã render xong hết trong lúc bridge
    tắt/restart sẽ bị bỏ quên vĩnh viễn nếu akali chưa kịp thu hồi lease để cấp
    lại (gặp thật: job #80 render xong 10/10, upload Drive xong xuôi, nhưng
    không ai gọi /complete vì bridge quay lại claim job #81 mới trước khi lease
    #80 hết hạn). Nếu lease đã hết hạn thật, _wait_for_batch phát hiện ngay ở
    heartbeat đầu tiên (409 -> lost_lease) và bỏ qua, không lặp vô hạn."""
    with _connect() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM akali_batches WHERE status = 'running'"
        ).fetchall()]
    for row in rows:
        if not _RUNNING:
            return
        _log(f"job #{row['akali_job_id']}: batch {row['batch_id']} còn dở từ lần chạy trước — nối lại theo dõi")
        _finish_batch(row["akali_job_id"], row["claim_token"], row["batch_id"], row["excel_stem"])


# ---------------------------------------------------------------- single-instance lock (giống queue_worker.py)

def _acquire_worker_lock():
    global _LOCK_FILE
    lock_file = open(WORKER_LOCK_PATH, "a+")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.seek(0)
        owner = lock_file.read().strip() or "unknown"
        lock_file.close()
        _log(f"another akali_bridge is already running (pid={owner}); exiting")
        return False
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    _LOCK_FILE = lock_file
    return True


def _stop(*_):
    global _RUNNING
    _RUNNING = False
    _log("shutdown signal received; finishing current cycle...")


# ---------------------------------------------------------------- vòng lặp chính

def main():
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    if not _acquire_worker_lock():
        return
    # Node key = "đăng nhập" của máy. Chưa kích hoạt thì không claim job của akali.
    if not dashboard_reporter.wait_until_activated(lambda: _RUNNING, _log):
        return
    if not bridge_enabled():
        _log("thiếu AKALI_URL/AKALI_WORKER_KEY trong .env — dừng (xem mục 8)")
        return

    db.init_db()
    tts_db.init_db()
    _init_batches_table()
    _log(f"bridge started; worker_id={_worker_id()} url={_base_url()}")

    # Bước 0 (mục 6) — đối soát TRƯỚC khi claim, rồi mới nối lại batch còn dở.
    # Thứ tự này quan trọng: đối soát dọn sạch những batch akali không còn công
    # nhận, để _resume_pending_batches() không đi heartbeat đúng mấy job đó.
    next_reconcile = 0.0
    while _RUNNING:
        try:
            if time.monotonic() >= next_reconcile:
                _reconcile()
                next_reconcile = time.monotonic() + _reconcile_seconds()
                _resume_pending_batches()

            # Bước 1 — rảnh? Máy 1 GPU, đừng claim khi queue_worker đang render job
            # tay (mục 8.5: claim bấm giờ lease ngay, xếp hàng chờ là cháy lease).
            if db.worker_busy():
                time.sleep(_poll_seconds())
                continue

            job = claim()
            if job is None:
                time.sleep(_poll_seconds())
                continue

            _log(f"claimed job #{job['id']}")
            _handle_job(job)
        except requests.RequestException as e:
            _log(f"lỗi mạng gọi akali (tiếp tục vòng sau): {e}")
            time.sleep(_poll_seconds())
        except Exception as e:
            _log(f"loop error (continuing): {e}")
            time.sleep(_poll_seconds())

    _log("bridge stopped.")


if __name__ == "__main__":
    main()
