"""Web UI for the LatentSync render queue (Gradio 5, 2 tabs).

Tab 1 — Render: upload video + audio, full config (như bản gradio_app cũ), rồi bấm
  "▶ Render (vào hàng đợi)" / "➕ Thêm vào hàng đợi" — cả hai đều đẩy job (kèm toàn bộ config)
  cho queue_worker.py render NỀN, TUẦN TỰ (một job xong mới tới job kế -> không tranh GPU).
Tab 2 — Trạng thái queue: bảng auto-refresh 10s + xem/tải video đã render.

Run with:  conda activate latentsync && python web_ui.py
"""
import os
import re
import shutil
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path

import gradio as gr

import database as db
import excel_import
import tts_config
from latentsync.tts.factory import available_providers, list_voices
from latentsync.tts.vbee import VbeeTTS
from latentsync.tts.ausynclab import AusynclabTTS
from render_job import OUT_RES  # bảng độ phân giải; render THỰC do queue_worker chạy nền
from stream_ui import (
    enqueue_audio as stream_enqueue_audio,
    interrupt_stream as stream_interrupt,
    preview_stream as stream_preview,
    refresh_stream as stream_refresh,
    start_stream as stream_start,
    stop_stream as stream_stop,
)

# Provider list for the Import-Excel tab dropdowns (label shows config status).
_TTS_PROVIDERS = available_providers()
TTS_PROVIDER_CHOICES = [
    (f"{p['label']}{'' if p['enabled'] else ' (chưa cấu hình)'}", p["name"])
    for p in _TTS_PROVIDERS
]
_TTS_DEFAULT = next((p["name"] for p in _TTS_PROVIDERS if p["is_default"]),
                    (_TTS_PROVIDERS[0]["name"] if _TTS_PROVIDERS else None))

ROOT = Path(__file__).parent
UPLOADS_DIR = ROOT / "uploads"

# Bảo đảm bảng tồn tại trước khi Blocks query giá trị khởi tạo.
db.init_db()
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

STATUS_LABEL = {
    "queued": "⏳ Chờ",
    "rendering": "🎬 Đang render",
    "done": "✅ Xong",
    "failed": "❌ Lỗi",
}


def _safe_stem(name):
    # NFC trước: gộp dấu tổ hợp tiếng Việt -> ký tự dựng sẵn để \w giữ được chữ có dấu (khớp _safe_name).
    name = unicodedata.normalize("NFC", (name or "").strip())
    return re.sub(r"[^\w.-]+", "_", name) or "job"


# Quy tắc đặt tên chuẩn để hệ live tự match video↔sản phẩm khi import.
# Tên file xuất = "<sản phẩm>__<INTENT>"  (bỏ __INTENT = video giới thiệu).
KIND_INTRO = "Giới thiệu sản phẩm"
KIND_ANSWER = "Trả lời câu hỏi"
# (nhãn hiển thị, mã intent). Mã PHẢI trùng intents bên hệ live (ASK_*).
INTENT_CHOICES = [
    ("Hỏi giá", "ASK_PRICE"),
    ("Hỏi chất lượng / review", "ASK_QUALITY"),
    ("Hỏi cách dùng", "ASK_USAGE"),
    ("Còn hàng / còn size-màu", "ASK_STOCK"),
    ("Cách mua / chốt đơn", "ASK_BUY"),
    ("Phí ship / giao hàng", "ASK_SHIPPING"),
    ("Voucher / giảm giá", "ASK_VOUCHER"),
    ("Đổi trả / bảo hành", "ASK_RETURN"),
    ("Xem sản phẩm", "ASK_PRODUCT"),
    ("Chất liệu / lớp / quai đeo", "ASK_MATERIAL"),
    ("Size / form / cân nặng", "ASK_SIZE_FIT"),
    ("Hạn sử dụng", "ASK_EXPIRY"),
    ("Dành cho trẻ em", "ASK_CHILDREN"),
    ("Màu sắc / mix màu", "ASK_COLOR"),
    ("Kiểm hàng", "ASK_CHECK"),
    ("Khác (other_key)", "ASK_OTHER"),
]


def _slug_key(value):
    key = excel_import._ascii(value).replace(" ", "_").replace("-", "_")
    key = re.sub(r"[^a-z0-9_]+", "_", key)
    key = re.sub(r"_+", "_", key).strip("_")
    return key or None


def _manual_intent(intent, other_key=None):
    if intent == "ASK_OTHER":
        key = _slug_key(other_key)
        return f"ASK_OTHER_{key}" if key else "ASK_OTHER"
    return intent


def build_name(product, kind, intent, other_key=None):
    """Dựng tên chuẩn từ ô nhập: '<sản phẩm>__<INTENT>' (Trả lời) hoặc '<sản phẩm>' (Giới thiệu)."""
    product = (product or "").strip()
    if kind == KIND_ANSWER and intent:
        intent = _manual_intent(intent, other_key)
        return f"{product}__{intent}" if product else f"__{intent}"
    return product or "video"


def name_preview(product, kind, intent, other_key=None):
    return f"📄 Tên file xuất: **{_safe_stem(build_name(product, kind, intent, other_key))}.mp4**"


def other_key_visibility(kind, intent):
    return gr.update(visible=(kind == KIND_ANSWER and intent == "ASK_OTHER"))


def input_type_preset(input_type):
    """Keep visible controls honest; render_job enforces the same safe bounds."""
    if input_type == "ai":
        return gr.update(value=1.3), gr.update(value=28), gr.update(value=False)
    return gr.update(value=1.5), gr.update(value=24), gr.update(value=True)


# ---------------------------------------------------------------- Thêm vào queue

def add_to_queue(video_path, audio_path, product, kind, intent, other_key, model_res, guidance, steps, seed,
                 enhance_mouth, enhance_region, out_res, input_type):
    if not video_path or not audio_path:
        raise gr.Error("Cần cả video và audio.")
    if kind == KIND_ANSWER and intent == "ASK_OTHER" and not _slug_key(other_key):
        raise gr.Error("Chọn ASK_OTHER thì cần điền other_key, ví dụ: khautrang.")
    name = build_name(product, kind, intent, other_key)   # tên chuẩn <sp>__<INTENT>
    job_dir = UPLOADS_DIR / uuid.uuid4().hex[:12]
    job_dir.mkdir(parents=True, exist_ok=True)
    # Copy uploads tới chỗ ổn định (file temp của Gradio bị dọn khi thoát).
    v_dst = job_dir / ("video" + Path(video_path).suffix)
    a_dst = job_dir / ("audio" + Path(audio_path).suffix)
    shutil.copy(video_path, v_dst)
    shutil.copy(audio_path, a_dst)
    job_id = db.add_job(name, v_dst, a_dst, model_res, guidance, steps, seed,
                        int(bool(enhance_mouth)), enhance_region, out_res, input_type)
    gr.Info(f"✅ Đã thêm job #{job_id} vào queue.")   # toast thông báo
    return f"✅ Đã thêm **job #{job_id}** ('{name}') vào hàng đợi **MuseTalk 1.5**."


# ---------------------------------------------------------------- Tab 2 helpers

def _parse_ts(ts):
    """Chuỗi 'YYYY-MM-DD HH:MM:SS' (datetime('now','localtime') trong DB) -> datetime, hoặc None."""
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def _fmt_secs(secs):
    """Số giây -> chuỗi gọn: '45s' / '5p 23s' / '1h 05p'."""
    secs = int(round(secs))
    if secs < 0:
        return ""
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}p"
    if m:
        return f"{m}p {s:02d}s"
    return f"{s}s"


def _job_duration(j):
    """Thời gian render. Xong/lỗi -> tổng (started->finished); đang render -> đã chạy '…'; chờ -> ''."""
    started = _parse_ts(j.get("started_at"))
    if not started:
        return ""
    if j["status"] == db.STATUS_RENDERING:
        return _fmt_secs((datetime.now() - started).total_seconds()) + "…"
    finished = _parse_ts(j.get("finished_at"))
    if finished:
        return _fmt_secs((finished - started).total_seconds())
    return ""


def _table_rows(prioritized_id=None):
    rows = []
    jobs = db.list_jobs(limit=200)
    rendering = [j for j in jobs if j["status"] == db.STATUS_RENDERING]
    queued = sorted(
        (j for j in jobs if j["status"] == db.STATUS_QUEUED),
        key=lambda j: (-int(j.get("priority") or 0), j.get("created_at") or "", j["id"]),
    )
    history = [j for j in jobs if j["status"] not in (db.STATUS_RENDERING, db.STATUS_QUEUED)]
    for j in [*rendering, *queued, *history]:
        rows.append([
            j["id"], j["name"], j["model_res"],
            STATUS_LABEL.get(j["status"], j["status"]),
            int(j.get("priority") or 0), _job_duration(j),
            j["retries"], j["created_at"] or "",
            (j["error"] or "")[:80],
            ("✅ Đã ưu tiên" if j["id"] == prioritized_id else "⚡ Ưu tiên")
            if j["status"] == db.STATUS_QUEUED else "",
        ])
    return rows


def _priority_choices():
    """Các job đang chờ theo đúng thứ tự queue hiện tại."""
    jobs = [j for j in db.list_jobs(limit=200) if j["status"] == db.STATUS_QUEUED]
    jobs.sort(key=lambda j: (-int(j.get("priority") or 0), j.get("created_at") or "", j["id"]))
    return [(f"#{j['id']} · {j['name']}", str(j["id"])) for j in jobs]


def _video_choices():
    """Video đã render trong thư mục Desktop — nguồn XEM LẠI, đọc THẲNG từ ổ đĩa nên KHÔNG mất
    khi xóa job khỏi queue. Mới nhất lên đầu (theo mtime). label = tên file (không .mp4)."""
    d = db.RENDERS_DIR
    if not d.exists():
        return {}
    files = sorted(d.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    return {p.stem: str(p) for p in files}


def _latest_video():
    """(label, path) của video mới render nhất, hoặc (None, None) nếu chưa có."""
    ch = _video_choices()
    if not ch:
        return None, None
    label = next(iter(ch))
    return label, ch[label]


def refresh_status():
    return (
        gr.update(value=_table_rows()),
        gr.update(choices=list(_video_choices().keys())),
    )


def pick_done(label=None):
    """Chọn 1 video đã render -> trả (video preview, file tải về)."""
    path = _video_choices().get(label) if label else None
    return path, path


def load_video_area():
    """demo.load: nạp video MỚI NHẤT vào khu xem lại của Tab 1 (để không để trống)."""
    label, path = _latest_video()
    return gr.update(choices=list(_video_choices().keys()), value=label), path, path


def _cleanup_job_files(row):
    """Best-effort: dọn file input đã upload của job (GIỮ lại video kết quả trên Desktop)."""
    try:
        vp = Path(row.get("video_path") or "")
        if (
            vp.exists()
            and UPLOADS_DIR in vp.parents
            and vp.parent.parent == UPLOADS_DIR
            and vp.parent.name not in {"videos", "tts"}
        ):   # uploads/<hex>/video.ext -> xóa cả thư mục tạm; không xóa uploads/videos hoặc uploads/tts
            shutil.rmtree(vp.parent, ignore_errors=True)
    except Exception:
        pass


def prioritize_from_table(table_data, evt: gr.SelectData):
    """Bấm ô thao tác ở dòng queued để ưu tiên ngay, không cần nhập ID."""
    try:
        index = evt.index if isinstance(evt.index, (list, tuple)) else ()
        row_index = int(index[0]) if len(index) > 1 else None
        col = int(index[1]) if len(index) > 1 else None
        if col != 9 or evt.value != "⚡ Ưu tiên":
            return gr.update(), gr.update()

        # Gradio 5 không cung cấp row_value cho Dataframe.select; đọc ID từ
        # chính dữ liệu bảng theo chỉ số dòng mà sự kiện gửi về.
        if row_index is None:
            raise ValueError("Không xác định được dòng job.")
        if hasattr(table_data, "iloc"):
            job_id = table_data.iloc[row_index, 0]
        elif isinstance(table_data, dict):
            job_id = table_data.get("data", [])[row_index][0]
        else:
            job_id = table_data[row_index][0]
        row = db.prioritize_job(int(job_id))
    except (TypeError, ValueError, IndexError, KeyError) as e:
        gr.Warning(str(e))
        return gr.update(), f"⚠ Không thể ưu tiên job: {e}"
    gr.Info(f"✅ Đã ưu tiên job #{row['id']}.")
    return gr.update(value=_table_rows(prioritized_id=row["id"])), (
        f"### ✅ Đã ưu tiên job #{row['id']}\n"
        "Job này đã được đưa lên đầu hàng chờ và sẽ chạy ngay sau job đang render."
    )


def apply_priority_list(job_ids):
    """Áp dụng thứ tự ưu tiên đã chọn. Outputs: bảng, thông báo, dropdown."""
    try:
        ordered_ids = db.prioritize_jobs(job_ids)
    except (TypeError, ValueError) as e:
        gr.Warning(str(e))
        return gr.update(), f"⚠ {e}", gr.update(choices=_priority_choices())
    order_text = " → ".join(f"#{job_id}" for job_id in ordered_ids)
    gr.Info("✅ Đã lưu danh sách ưu tiên.")
    return (
        gr.update(value=_table_rows(prioritized_id=ordered_ids[0])),
        f"### ✅ Đã lưu danh sách ưu tiên\nThứ tự chạy: **{order_text}**",
        gr.update(choices=_priority_choices(), value=[str(job_id) for job_id in ordered_ids]),
    )


def do_prioritize_job(job_id):
    """Đưa job queued lên đầu hàng đợi. Outputs: [status_table, done_dd, message]."""
    if not job_id:
        gr.Warning("Nhập ID job cần ưu tiên.")
        return gr.update(), gr.update(), "⚠ Nhập ID job cần ưu tiên."
    try:
        row = db.prioritize_job(int(job_id))
    except (TypeError, ValueError) as e:
        gr.Warning(str(e))
        return gr.update(), gr.update(), f"⚠ {e}"
    gr.Info(f"⚡ Job #{row['id']} sẽ được render kế tiếp.")
    table, dd = refresh_status()
    return table, dd, f"⚡ Đã đưa job #{row['id']} ('{row['name']}') lên đầu queue."


def do_delete_job(job_id):
    """Xóa 1 job theo ID. Outputs: [status_table, done_dd, del_msg]."""
    if not job_id:
        gr.Warning("Nhập ID job cần xóa.")
        return gr.update(), gr.update(), "⚠ Nhập ID job cần xóa."
    try:
        row = db.delete_job(int(job_id))
    except ValueError as e:                  # job đang render
        gr.Warning(str(e))
        return gr.update(), gr.update(), f"⚠ {e}"
    if not row:
        gr.Warning(f"Không thấy job #{int(job_id)}.")
        return gr.update(), gr.update(), f"⚠ Không thấy job #{int(job_id)}."
    _cleanup_job_files(row)
    gr.Info(f"✅ Đã xóa job #{row['id']}.")
    table, dd = refresh_status()
    return table, dd, f"✅ Đã xóa job #{row['id']} ('{row['name']}')."


def do_clear(status_key, label):
    """Xóa hàng loạt job theo trạng thái. Outputs: [status_table, done_dd, del_msg]."""
    try:
        rows = db.clear_jobs(status_key)
    except ValueError as e:
        gr.Warning(str(e))
        return gr.update(), gr.update(), f"⚠ {e}"
    for r in rows:
        _cleanup_job_files(r)
    gr.Info(f"✅ Đã xóa {len(rows)} job {label}.")
    table, dd = refresh_status()
    return table, dd, f"✅ Đã xóa {len(rows)} job {label}."


# ---------------------------------------------------------------- Tab Import Excel

def make_template_file():
    """Sinh file template.xlsx (header + 3 ví dụ + comment) để tải về."""
    path = str(ROOT / "template.xlsx")
    return excel_import.make_template(path)


def update_voice_choices(provider):
    """Đổi provider -> nạp danh sách giọng gợi ý (cho phép tự gõ thêm).

    list_voices có thể trả (nhãn, code) hoặc code thuần -> value phải là code."""
    vs = list_voices(provider)
    first = vs[0] if vs else None
    val = first[1] if isinstance(first, tuple) else first
    return gr.update(choices=vs, value=val)


def _preview_rows_table(rows, errors, shopee_item_id=None):
    table = []
    for r in rows:
        out_name = excel_import.build_name_excel(
            r["product"], r["video_type"], r["question_type"], r.get("other_key"), r["row"])
        table.append([r["row"], r["product"] or "(chung)",
                      r.get("keyword") or "", r["question_type"] or "",
                      r.get("other_key") or "", r.get("question_text") or "",
                      Path(r["video_path"]).name, out_name + ".mp4",
                      r["tts_provider"] or "(mặc định)", "✅ Ready"])
    for e in errors:
        table.append([e["row"], "—", "—", "—", "—", "—", "—", "—", "—", f"❌ {e['error']}"])
    return table


def _stabilize_video(path):
    """Video tải lên qua Gradio là file TẠM (bị dọn). Copy vào uploads/videos/ để worker render sau còn đọc được."""
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return str(p)
    vids = UPLOADS_DIR / "videos"
    vids.mkdir(parents=True, exist_ok=True)
    if vids in p.parents:                       # đã nằm trong kho bền rồi
        return str(p)
    dst = vids / (uuid.uuid4().hex[:12] + (p.suffix or ".mp4"))
    shutil.copy(p, dst)
    return str(dst)


def _stabilize_excel(path):
    """Copy file Excel tải lên (Gradio temp) -> uploads/imports/ để sau xuất kết quả còn đọc được.

    GIỮ NGUYÊN tên file gốc (chống trùng bằng thư mục con uuid) — basename này chính là
    tên thư mục con trên Google Drive của batch (drive_folder), đổi thành uuid là sai tên."""
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return str(p)
    d = UPLOADS_DIR / "imports" / uuid.uuid4().hex[:12]
    d.mkdir(parents=True, exist_ok=True)
    dst = d / (p.name or "import.xlsx")
    shutil.copy(p, dst)
    return str(dst)


def preview_excel(excel_file, default_video, default_provider, shopee_item_id):
    if not excel_file:
        raise gr.Error("Chưa chọn file Excel.")
    shopee = (shopee_item_id or "").strip() or None
    stable_default = _stabilize_video(default_video)   # copy video tải lên -> đường dẫn bền
    stable_excel = _stabilize_excel(excel_file)        # giữ bản Excel gốc để xuất kết quả
    rows, errors = excel_import.process_excel(
        stable_excel, stable_default, default_provider, shopee)
    table = _preview_rows_table(rows, errors, shopee)
    summary = (f"**Tổng: {len(rows) + len(errors)} | "
               f"Sẵn sàng: {len(rows)} | Lỗi: {len(errors)}**")
    state = {"rows": rows, "shopee": shopee, "excel_path": stable_excel}
    gr.Info(f"Preview: {len(rows)} sẵn sàng, {len(errors)} lỗi.")
    return table, summary, state


def submit_excel(state, model, out_res, enhance_mouth, guidance, steps, seed, region, input_type,
                 dedup, progress=gr.Progress()):
    if not state or not state.get("rows"):
        raise gr.Error("Chưa có dòng sẵn sàng — bấm Preview trước.")
    rows = state["rows"]
    shopee = state.get("shopee")
    render_config = dict(model_res=str(model), out_res=str(out_res),
                         enhance_mouth=int(bool(enhance_mouth)), enhance_region=region,
                         guidance=float(guidance), steps=int(steps), seed=int(seed), input_type=input_type)

    def _cb(done, total, label):
        progress(done / max(1, total), desc=label)

    enqueued, warnings, batch_id, skipped = excel_import.submit_jobs(
        rows, shopee_item_id=shopee, progress=_cb, excel_path=state.get("excel_path"),
        render_config=render_config, dedup=bool(dedup))
    lines = [f"### ✅ Đã đưa **{len(enqueued)}** dòng vào TTS queue (batch `{batch_id}`).",
             f"⚙️ Engine **MuseTalk 1.5** · đầu ra **{out_res}** · "
             f"{'làm nét '+region if enhance_mouth else 'KHÔNG làm nét'} · guidance {guidance} · {steps} steps**.",
             "TTS worker sẽ tạo giọng (rate-limit + tự retry khi nghẽn) rồi đẩy sang hàng đợi render — "
             "không còn rớt dòng vì lỗi tạm thời. Theo dõi ở mục **Trạng thái TTS** bên dưới."]
    if skipped:
        lines.append(f"\n⏭ **Bỏ qua {len(skipped)} dòng TRÙNG** (đã có job chờ/đang chạy — "
                     "tránh render lại cùng nội dung):")
        for s in skipped[:15]:
            lines.append(f"- dòng {s['row']}: '{s['name']}' (trùng tts#{s['dup_id']})")
        if len(skipped) > 15:
            lines.append(f"- … và {len(skipped) - 15} dòng nữa")
    if warnings:
        lines.append(f"\n⚠ **{len(warnings)} dòng được sửa voice** (voice guard):")
        for w in warnings:
            lines.append(f"- dòng {w['row']}: {w['warn']}")
    lines.append("\n*Render xong, bấm **📥 Tải Excel kết quả** để lấy file đã điền `video_done`.*")
    gr.Info(f"Enqueue {len(enqueued)} dòng, bỏ {len(skipped)} dòng trùng.")
    return "\n".join(lines), gr.update(value=_table_rows()), batch_id


def export_excel_results(batch_id):
    """Xuất Excel gốc đã điền 'video_done' cho các dòng render xong. Trả file để tải."""
    import tts_db
    if not batch_id:
        lb = tts_db.latest_batch()
        batch_id = lb["batch_id"] if lb else None
    if not batch_id:
        raise gr.Error("Chưa có batch import nào để xuất.")
    out, filled, total = excel_import.export_results(batch_id)
    gr.Info(f"Đã điền video_done cho {filled}/{total} dòng (dòng chưa render xong để trống).")
    return out


_TTS_LABEL = {"pending": "⏳ chờ", "submitting": "🔄 đang chạy", "retry_wait": "🔁 chờ retry",
              "done": "✅ xong", "failed_retryable": "🟠 dead-letter", "failed_permanent": "❌ lỗi cứng"}


def tts_status_md():
    """Tóm tắt TTS queue + vài dòng dead-letter gần nhất (cho mục Trạng thái TTS)."""
    import tts_db
    counts = tts_db.status_counts()
    if not counts:
        return "**TTS queue:** (trống)"
    order = ["pending", "submitting", "retry_wait", "done", "failed_retryable", "failed_permanent"]
    parts = [f"{_TTS_LABEL.get(k, k)}: **{counts[k]}**" for k in order if k in counts]
    md = "**TTS queue** — " + " · ".join(parts)
    dead = tts_db.list_jobs(limit=5, status="failed_retryable")
    if dead:
        md += "\n\n*Dead-letter gần đây:*"
        for d in dead:
            md += f"\n- dòng {d['excel_row']}: {str(d['last_error'])[:90]}"
    return md


def requeue_dead_letter():
    import tts_db
    n = tts_db.requeue_dead_letter()
    gr.Info(f"Đưa lại {n} dòng dead-letter vào queue." if n else "Không có dòng dead-letter.")
    return tts_status_md()


# ---------------------------------------------------------------- Tab Cấu hình TTS

def save_tts_config(default_provider, *vals):
    """Lưu cấu hình TTS vào .env + áp dụng ngay (reset factory). Outputs: [status_md, provider_dropdowns...]."""
    values = dict(zip(tts_config.ALL_KEYS, vals))
    providers = tts_config.save_config(values, default_provider)
    md = "### ✅ Đã lưu cấu hình TTS\n\n" + tts_config.status_markdown(providers)
    # Cập nhật lại dropdown provider mặc định ở tab Import (nhãn (chưa cấu hình) có thể đổi).
    choices = [(f"{p['label']}{'' if p['enabled'] else ' (chưa cấu hình)'}", p["name"]) for p in providers]
    fresh = tts_config.current_values()
    input_updates = [
        gr.update(value=fresh.get(k, "") or ("1.2" if k == "TTS_SPEED" else ""))
        for k in tts_config.ALL_KEYS
    ]
    gr.Info("Đã lưu .env và áp dụng cấu hình TTS.")
    return md, gr.update(choices=choices, value=default_provider), gr.update(value=default_provider), *input_updates


def load_tts_config_form():
    """Refresh config form values from .env on browser load/reload."""
    fresh = tts_config.current_values()
    default_provider = tts_config.current_default_provider()
    input_updates = [
        gr.update(value=fresh.get(k, "") or ("1.2" if k == "TTS_SPEED" else ""))
        for k in tts_config.ALL_KEYS
    ]
    return gr.update(value=default_provider), *input_updates, tts_config.status_markdown()


def vbee_connect(app_id, token):
    """Gọi Vbee lấy danh sách giọng (App ID + Token đang gõ, chưa cần Lưu)."""
    from latentsync.tts.vbee import VbeeTTS
    try:
        voices = VbeeTTS(app_id=app_id, token=token).fetch_voices()
    except Exception as e:
        raise gr.Error(f"Kết nối Vbee lỗi: {e}")
    codes = [v["code"] for v in voices if v.get("code")]
    gr.Info(f"Đã tải {len(codes)} giọng Vbee.")
    return gr.update(choices=codes, value=(codes[0] if codes else None)), f"✅ Tải **{len(codes)}** giọng."


def vbee_test(app_id, token, voice, text):
    """Nghe thử end-to-end: synth 1 câu bằng credential đang gõ -> trả file audio."""
    from latentsync.tts.vbee import VbeeTTS
    if not (text or "").strip():
        raise gr.Error("Nhập câu cần đọc thử.")
    out = str(UPLOADS_DIR / "vbee_test.wav")
    try:
        VbeeTTS(app_id=app_id, token=token, default_voice=voice or None).synthesize(text, out, voice or None)
    except Exception as e:
        raise gr.Error(f"Nghe thử lỗi: {e}")
    return out


def ausynclab_connect(api_key):
    """Gọi AusyncLab lấy danh sách voice (API key đang gõ). Nhãn dễ đọc, value = voice id."""
    from latentsync.tts.ausynclab import AusynclabTTS
    try:
        voices = AusynclabTTS(api_key=api_key).fetch_voices()
    except Exception as e:
        raise gr.Error(f"Kết nối AusyncLab lỗi: {e}")
    choices = []
    for v in voices:
        if not v.get("code"):
            continue
        meta = " · ".join(x for x in (v.get("language_code"), v.get("gender"), v.get("use_case")) if x)
        label = f"{v.get('name') or 'voice'}" + (f" ({meta})" if meta else "") + f" · #{v['code']}"
        choices.append((label, v["code"]))
    gr.Info(f"Đã tải {len(choices)} voice AusyncLab.")
    return gr.update(choices=choices, value=(choices[0][1] if choices else None)), f"✅ Tải **{len(choices)}** voice."


def ausynclab_test(api_key, voice, text):
    from latentsync.tts.ausynclab import AusynclabTTS
    if not (text or "").strip():
        raise gr.Error("Nhập câu cần đọc thử.")
    out = str(UPLOADS_DIR / "ausynclab_test.wav")
    try:
        AusynclabTTS(api_key=api_key, default_voice=voice or None).synthesize(text, out, voice or None)
    except Exception as e:
        raise gr.Error(f"Nghe thử lỗi: {e}")
    return out


def autovoice_connect(api_key, url, voices_url):
    """Tai danh sach voice he thong neu endpoint /voices san sang."""
    from latentsync.tts.autovoice import AutoVoiceTTS
    try:
        voices = AutoVoiceTTS(api_key=api_key, url=url or None, voices_url=voices_url or None).fetch_voices()
    except Exception as e:
        raise gr.Error(f"Kết nối Voice hệ thống lỗi: {e}")
    choices = []
    for v in voices:
        if not v.get("code"):
            continue
        meta = " · ".join(x for x in (v.get("language"), v.get("gender")) if x)
        label = f"{v.get('name') or 'voice'}" + (f" ({meta})" if meta else "") + f" · #{v['code']}"
        choices.append((label, v["code"]))
    gr.Info(f"Đã tải {len(choices)} voice hệ thống.")
    return gr.update(choices=choices, value=(choices[0][1] if choices else None)), f"✅ Tải **{len(choices)}** voice."


def autovoice_test(api_key, voice, url, speed, text):
    from latentsync.tts.autovoice import AutoVoiceTTS
    if not (text or "").strip():
        raise gr.Error("Nhập câu cần đọc thử.")
    out = str(UPLOADS_DIR / "autovoice_test.wav")
    try:
        AutoVoiceTTS(api_key=api_key, default_voice=voice or None,
                     url=url or None, speed=(speed or None)).synthesize(text, out, voice or None)
    except Exception as e:
        raise gr.Error(f"Nghe thử lỗi: {e}")
    return out


CSS = """
#name-box {
  border: 2px solid #f59e0b;
  border-radius: 12px;
  padding: 14px 16px 8px;
  background: rgba(245, 158, 11, 0.07);
  box-shadow: 0 0 0 3px rgba(245, 158, 11, 0.10);
}
#name-box label { font-weight: 600; }
#name-preview { font-size: 1.1rem; }
#priority-list {
  margin: 8px 0 14px;
  padding: 14px;
  border: 1px solid rgba(37, 99, 235, 0.35);
  border-radius: 12px;
  background: rgba(37, 99, 235, 0.06);
}
#priority-feedback:not(:empty) {
  margin: 8px 0 12px;
  padding: 12px 16px;
  border: 1px solid #16a34a;
  border-radius: 10px;
  background: rgba(22, 163, 74, 0.12);
  color: #15803d;
}
#queue-table table tbody tr td:last-child {
  text-align: center;
  cursor: pointer;
  white-space: nowrap;
}
#queue-table table tbody tr td:last-child span:not(:empty) {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 92px;
  padding: 6px 12px;
  color: #9a3412;
  background: linear-gradient(180deg, #fff7ed 0%, #ffedd5 100%);
  border: 1px solid #fdba74;
  border-radius: 999px;
  font-size: 0.84rem;
  font-weight: 700;
  line-height: 1.2;
  box-shadow: 0 1px 2px rgba(154, 52, 18, 0.10);
  transition: all 140ms ease;
}
#queue-table table tbody tr td:last-child:hover span:not(:empty) {
  color: #ffffff;
  background: linear-gradient(180deg, #f97316 0%, #ea580c 100%);
  border-color: #ea580c;
  box-shadow: 0 4px 10px rgba(234, 88, 12, 0.25);
  transform: translateY(-1px);
}
#queue-table table tbody tr td:last-child:active span:not(:empty) {
  box-shadow: 0 1px 3px rgba(234, 88, 12, 0.20);
  transform: translateY(0) scale(0.98);
}
"""

UPLOAD_PROGRESS_FIX_JS = r"""
() => {
  if (window.__renderVideoUploadFix) return;
  window.__renderVideoUploadFix = true;
  const originalFetch = window.fetch.bind(window);
  let uploadId = null;
  window.fetch = (input, init) => {
    const raw = typeof input === "string" ? input : (input instanceof URL ? input.href : input.url);
    const url = new URL(raw, window.location.href);
    if (url.pathname.endsWith("/gradio_api/upload_progress") && url.searchParams.get("upload_id") === "undefined") {
      uploadId = uploadId || Math.random().toString(36).slice(2, 15);
      url.searchParams.set("upload_id", uploadId);
      input = input instanceof Request ? new Request(url.toString(), input) : url.toString();
    } else if (url.pathname.endsWith("/gradio_api/upload") && url.searchParams.has("upload_id")) {
      if (uploadId) {
        url.searchParams.set("upload_id", uploadId);
        input = input instanceof Request ? new Request(url.toString(), input) : url.toString();
      } else {
        uploadId = url.searchParams.get("upload_id");
      }
    }
    return originalFetch(input, init);
  };
}
"""

with gr.Blocks(title="Render Queue", css=CSS, js=UPLOAD_PROGRESS_FIX_JS) as demo:
    gr.Markdown("<h1 align='center'>Render Queue 24/7</h1>")

    with gr.Tab("🎬 Render / Tạo job"):
        with gr.Row():
            with gr.Column():
                video_in = gr.Video(label="Video")
                audio_in = gr.Audio(label="Audio", type="filepath")

                # Đặt tên CHUẨN để hệ live tự match video↔sản phẩm khi import — phần QUAN TRỌNG nhất.
                with gr.Group(elem_id="name-box"):
                    gr.Markdown(
                        "### ⭐ ĐẶT TÊN VIDEO — *quan trọng*\n"
                        "Hệ live khớp video ↔ sản phẩm **theo tên này**. Đặt đúng để tự động match.")
                    product_in = gr.Textbox(
                        label="Sản phẩm (tên hoặc Shopee item_id)",
                        placeholder="vd: set kep toc  hoặc  23525384022")
                    with gr.Row():
                        kind_in = gr.Radio([KIND_INTRO, KIND_ANSWER], value=KIND_INTRO,
                                           label="Loại video", scale=2)
                        intent_in = gr.Dropdown(choices=INTENT_CHOICES, label="Câu hỏi (khi Trả lời)",
                                                visible=False, scale=2)
                    other_key_in = gr.Textbox(
                        label="Other key (khi chọn ASK_OTHER)",
                        placeholder="vd: khautrang, combo_quatang",
                        visible=False)
                    name_preview_md = gr.Markdown(name_preview("", KIND_INTRO, None, None),
                                                  elem_id="name-preview")

                input_type_in = gr.Radio(
                    choices=[("Người thật", "real"), ("Video AI", "ai")], value="real",
                    label="Loại input (Video AI dùng mask môi hẹp, không GFPGAN)")
                with gr.Row():
                    model_in = gr.Radio(
                        choices=["256", "512"], value="512",
                        label="Model: 256 (nhanh ~2×) | 512 (nét/tự nhiên, chậm)",
                    )
                    out_res_in = gr.Radio(
                        choices=list(OUT_RES.keys()), value="720",
                        label="Độ phân giải (cạnh ngắn)",
                    )
                with gr.Row():
                    guidance_in = gr.Slider(1.0, 3.0, value=1.5, step=0.1, label="Guidance Scale")
                    steps_in = gr.Slider(8, 50, value=24, step=1, label="Inference Steps")
                with gr.Row():
                    seed_in = gr.Number(value=1247, label="Seed", precision=0)
                    enhance_mouth_in = gr.Checkbox(value=True, label="Làm nét miệng (GFPGAN)")
                    region_in = gr.Radio(["mouth", "face"], value="mouth", label="Vùng làm nét")
                input_type_in.change(
                    input_type_preset, input_type_in,
                    [guidance_in, steps_in, enhance_mouth_in])

                with gr.Row():
                    render_btn = gr.Button("▶ Render (vào hàng đợi)", variant="primary")
                    queue_btn = gr.Button("➕ Thêm vào hàng đợi", variant="secondary")
                msg = gr.Markdown()

            with gr.Column():
                gr.Markdown(
                    "### 📺 Video đã render — xem lại\n"
                    "Job chạy **tuần tự qua hàng đợi** (tiến trình ở tab **Trạng thái queue**). "
                    "Video xong **luôn lưu** ở `Desktop/Renders` — chọn bên dưới để xem lại.")
                _lbl0, _path0 = _latest_video()
                result_dd = gr.Dropdown(label="Chọn video (mới nhất ở đầu)",
                                        choices=list(_video_choices().keys()), value=_lbl0)
                result_video = gr.Video(label="Xem lại", value=_path0)
                result_dl = gr.File(label="Tải về", value=_path0)
                result_refresh = gr.Button("🔄 Cập nhật danh sách (hiện video mới render xong)")

        cfg_inputs = [video_in, audio_in, product_in, kind_in, intent_in, other_key_in, model_in, guidance_in,
                      steps_in, seed_in, enhance_mouth_in, region_in, out_res_in, input_type_in]
        # Hiện ô câu hỏi chỉ khi chọn "Trả lời".
        kind_in.change(lambda k: gr.update(visible=(k == KIND_ANSWER)), kind_in, intent_in)
        kind_in.change(other_key_visibility, [kind_in, intent_in], other_key_in)
        intent_in.change(other_key_visibility, [kind_in, intent_in], other_key_in)
        # Preview tên file xuất cập nhật TRỰC TIẾP khi gõ tên / đổi loại / đổi câu hỏi.
        _name_inputs = [product_in, kind_in, intent_in, other_key_in]
        product_in.input(name_preview, _name_inputs, name_preview_md)
        kind_in.change(name_preview, _name_inputs, name_preview_md)
        intent_in.change(name_preview, _name_inputs, name_preview_md)
        other_key_in.input(name_preview, _name_inputs, name_preview_md)
        # Cả HAI nút đều ĐẨY VÀO HÀNG ĐỢI (worker render nền, tuần tự — không còn render in-process
        # nên không tranh GPU). Báo NGAY "đang thêm" rồi mới copy file + thêm DB (kèm toast).
        render_btn.click(lambda: "⏳ Đang thêm vào hàng đợi…", None, msg).then(add_to_queue, cfg_inputs, msg)
        queue_btn.click(lambda: "⏳ Đang thêm vào hàng đợi…", None, msg).then(add_to_queue, cfg_inputs, msg)
        # Khu xem lại: chọn video -> phát + cho tải; nút 🔄 cập nhật danh sách + hiện video mới nhất.
        result_dd.input(pick_done, result_dd, [result_video, result_dl])
        result_refresh.click(load_video_area, None, [result_dd, result_video, result_dl])

    with gr.Tab("📊 Trạng thái queue"):
        gr.Markdown("Tự refresh mỗi 10 giây.")
        with gr.Group(elem_id="priority-list"):
            gr.Markdown("#### 📌 Tạo danh sách ưu tiên\nChọn các job theo thứ tự muốn chạy — job chọn đầu tiên sẽ chạy trước.")
            priority_list = gr.Dropdown(
                label="Thứ tự job ưu tiên", choices=_priority_choices(),
                multiselect=True, info="Có thể chọn nhiều job đang chờ.",
            )
            with gr.Row():
                priority_apply = gr.Button("✅ Áp dụng danh sách", variant="primary")
                priority_reload = gr.Button("🔄 Cập nhật job đang chờ")
        priority_msg = gr.Markdown(elem_id="priority-feedback")

        status_table = gr.Dataframe(
            headers=["ID", "Tên", "Model", "Trạng thái", "⚡ Ưu tiên", "⏱ Thời gian render", "Retry", "Tạo lúc", "Lỗi", "Thao tác"],
            datatype=["number", "str", "str", "str", "number", "str", "number", "str", "str", "str"],
            value=_table_rows(), interactive=False, wrap=True,
            elem_id="queue-table",
        )
        with gr.Row():
            done_dd = gr.Dropdown(label="Video đã render (xem / tải)",
                                  choices=list(_video_choices().keys()))
            download_file = gr.File(label="Tải về")
        done_video = gr.Video(label="Video đã render")
        # .input (not .change): chỉ kích hoạt khi NGƯỜI DÙNG chọn, tránh trigger rỗng
        # khi timer/load cập nhật lại choices (gây lỗi "got: 0").
        done_dd.input(pick_done, done_dd, [done_video, download_file])

        status_table.select(prioritize_from_table, status_table, [status_table, priority_msg])
        priority_apply.click(
            apply_priority_list, priority_list, [status_table, priority_msg, priority_list]
        )
        priority_reload.click(
            lambda: gr.update(choices=_priority_choices()), None, priority_list
        )

        gr.Markdown("#### 🗑 Thao tác — xóa job")
        with gr.Row():
            del_id = gr.Number(label="ID job cần xóa", precision=0, scale=1)
            del_btn = gr.Button("🗑 Xóa job này", variant="stop", scale=1)
            clear_failed_btn = gr.Button("Xóa hết job ❌ Lỗi", scale=1)
            clear_done_btn = gr.Button("Xóa hết job ✅ Xong", scale=1)
        del_msg = gr.Markdown()
        del_out = [status_table, done_dd, del_msg]
        del_btn.click(do_delete_job, del_id, del_out)
        clear_failed_btn.click(lambda: do_clear(db.STATUS_FAILED, "❌ Lỗi"), None, del_out)
        clear_done_btn.click(lambda: do_clear(db.STATUS_DONE, "✅ Xong"), None, del_out)

        timer = gr.Timer(10)
        timer.tick(refresh_status, outputs=[status_table, done_dd])
        timer.tick(lambda: gr.update(choices=_priority_choices()), None, priority_list)
        # Cập nhật danh sách video ở khu xem lại Tab 1 (chỉ choices -> không cắt ngang video đang phát).
        timer.tick(lambda: gr.update(choices=list(_video_choices().keys())), None, result_dd)

    with gr.Tab("📥 Import Excel"):
        gr.Markdown(
            "### Import hàng loạt từ Excel → TTS đa luồng → hàng đợi render\n"
            "Mỗi dòng = 1 job. TTS chạy **song song**; render vẫn **tuần tự** (1 GPU). "
            "Cấu hình render bên dưới áp cho **TOÀN BỘ** file Excel.")

        with gr.Row():
            tpl_btn = gr.Button("📥 Tải mẫu Excel")
            tpl_file = gr.File(label="template.xlsx")
        tpl_btn.click(make_template_file, None, tpl_file)

        gr.Markdown("#### Cấu hình mặc định (áp dụng cho ô để trống trong Excel)")
        with gr.Row():
            xl_default_video = gr.Video(
                label="Video mặc định (tải lên — dùng cho dòng để TRỐNG cột video_path)",
                sources=["upload"])
            xl_default_provider = gr.Dropdown(
                choices=TTS_PROVIDER_CHOICES, value=_TTS_DEFAULT,
                label="TTS Provider mặc định")
            xl_default_voice = gr.Dropdown(
                choices=list_voices(_TTS_DEFAULT), value=None,
                label="Giọng mặc định", allow_custom_value=True)
            xl_shopee = gr.Textbox(label="Shopee Item ID / Sản phẩm chung (fallback)",
                                   placeholder="dùng cho dòng để TRỐNG cột 'product'")
        xl_default_provider.change(update_voice_choices, xl_default_provider, xl_default_voice)

        gr.Markdown("#### ⚙️ Cấu hình render (áp cho TOÀN BỘ Excel)")
        xl_input_type = gr.Radio(
            choices=[("Người thật", "real"), ("Video AI", "ai")], value="real",
            label="Loại input")
        with gr.Row():
            xl_model = gr.Radio(["256", "512"], value="512",
                                label="Model: 256 (nhanh) | 512 (nét/tự nhiên, chậm)")
            xl_out_res = gr.Radio(choices=list(OUT_RES.keys()), value="720",
                                  label="Độ phân giải (cạnh ngắn): Gốc | 1080 | 720")
            xl_enhance = gr.Checkbox(value=True, label="Làm nét miệng (GFPGAN)")
            xl_region = gr.Radio(["mouth", "face"], value="mouth", label="Vùng làm nét")
        with gr.Row():
            xl_guidance = gr.Slider(1.0, 3.0, value=1.5, step=0.1, label="Guidance Scale")
            xl_steps = gr.Slider(8, 50, value=24, step=1, label="Inference Steps")
            xl_seed = gr.Number(value=1247, label="Seed", precision=0)
            xl_dedup = gr.Checkbox(
                value=True,
                label="Bỏ qua dòng đang chờ trùng (chống bấm Submit/import lặp)")
        xl_input_type.change(
            input_type_preset, xl_input_type,
            [xl_guidance, xl_steps, xl_enhance])

        gr.Markdown("#### Import")
        xl_file = gr.File(label="Upload file Excel (.xlsx)", file_types=[".xlsx"])
        with gr.Row():
            xl_preview_btn = gr.Button("🔍 Preview", variant="secondary")
            xl_submit_btn = gr.Button("🚀 Submit các job sẵn sàng", variant="primary")
        xl_summary = gr.Markdown()
        xl_table = gr.Dataframe(
            headers=["Row", "Sản phẩm", "Keyword", "ASK", "Other key", "Câu hỏi",
                     "Video", "Tên xuất (.mp4)", "Provider", "Status"],
            datatype=["number", "str", "str", "str", "str", "str", "str", "str", "str", "str"],
            interactive=False, wrap=True)
        xl_state = gr.State()
        xl_batch_state = gr.State()          # batch_id của lần submit gần nhất (để xuất kết quả)
        xl_result = gr.Markdown()

        gr.Markdown("#### 📨 Trạng thái TTS queue (tự cập nhật 10s)")
        xl_tts_status = gr.Markdown()
        with gr.Row():
            xl_tts_requeue = gr.Button("🔁 Chạy lại dead-letter (các dòng lỗi tạm thời)")
            xl_export_btn = gr.Button("📥 Tải Excel kết quả (điền video_done)", variant="primary")
        xl_export_file = gr.File(label="Excel kết quả (download)")

        xl_preview_btn.click(
            preview_excel,
            [xl_file, xl_default_video, xl_default_provider, xl_shopee],
            [xl_table, xl_summary, xl_state])
        # Submit: enqueue vào TTS queue (worker tự synth + đẩy render), refresh bảng trạng thái.
        xl_submit_btn.click(
            submit_excel,
            [xl_state, xl_model, xl_out_res, xl_enhance, xl_guidance, xl_steps, xl_seed,
             xl_region, xl_input_type, xl_dedup],
            [xl_result, status_table, xl_batch_state])
        xl_tts_requeue.click(requeue_dead_letter, None, xl_tts_status)
        xl_export_btn.click(export_excel_results, xl_batch_state, xl_export_file)
        timer.tick(tts_status_md, outputs=xl_tts_status)   # 'timer' định nghĩa ở tab Trạng thái queue

    with gr.Tab("📡 Livestream"):
        gr.Markdown(
            "### MuseTalk → ReLive → OBS\n"
            "Để trống Server URL và Stream key để MuseTalk trả MP4 cho ReLive, "
            "sau đó ReLive tự đưa clip vào OBS. Stream key chỉ dùng khi muốn đẩy RTMP trực tiếp."
        )
        with gr.Row():
            with gr.Column():
                live_session = gr.Textbox(
                    label="Session ID", value="facebook-live",
                    placeholder="vd: facebook-live")
                live_avatar = gr.File(
                    label="Avatar video (MP4)", file_types=["video"], type="filepath")
                live_server_url = gr.Textbox(
                    label="Facebook Server URL (chỉ RTMP trực tiếp)",
                    value="",
                    placeholder="Để trống khi dùng ReLive/OBS")
                live_stream_key = gr.Textbox(
                    label="Facebook Stream key (chỉ RTMP trực tiếp)", type="password",
                    placeholder="Để trống khi dùng ReLive/OBS",
                    info="Không cần stream key khi dùng ReLive; ô sẽ được xóa sau khi Start.")
                with gr.Row():
                    live_warmup = gr.Slider(0, 10, value=10, step=0.5, label="Render-ahead (giây)")
                    live_batch = gr.Slider(1, 32, value=20, step=1, label="MuseTalk batch size")
                live_audio_delay = gr.Slider(
                    0, 1000, value=300, step=50,
                    label="Audio delay (ms) — tăng khi hình chậm hơn tiếng")
                with gr.Row():
                    live_start_btn = gr.Button("▶ Start ReLive / OBS", variant="primary")
                    live_stop_btn = gr.Button("⏹ Stop stream", variant="stop")

            with gr.Column():
                live_preview = gr.Image(
                    label="Preview đang phát", interactive=False, height=480)
                live_audio = gr.File(
                    label="Audio câu tiếp theo", file_types=["audio"], type="filepath")
                live_request = gr.Textbox(
                    label="Request ID (có thể để trống)", placeholder="vd: sentence-001")
                with gr.Row():
                    live_priority = gr.Number(label="Priority", value=10, precision=0)
                    live_interrupt_next = gr.Checkbox(
                        label="Ngắt câu hiện tại trước khi phát", value=False)
                with gr.Row():
                    live_enqueue_btn = gr.Button("🔊 Phát audio", variant="primary")
                    live_interrupt_btn = gr.Button("⏭ Ngắt câu", variant="secondary")
                    live_refresh_btn = gr.Button("🔄 Status")
                live_status_md = gr.Markdown("### Trạng thái: chưa chạy")
                live_status_json = gr.JSON(label="Streaming metrics")
                live_preview_timer = gr.Timer(1.0)

        live_start_btn.click(
            stream_start,
            [live_session, live_avatar, live_server_url, live_stream_key, live_warmup, live_batch, live_audio_delay],
            [live_status_md, live_status_json, live_stream_key],
        )
        live_enqueue_btn.click(
            stream_enqueue_audio,
            [live_session, live_request, live_audio, live_priority, live_interrupt_next],
            [live_status_md, live_status_json],
        )
        live_refresh_btn.click(stream_refresh, live_session, [live_status_md, live_status_json])
        live_interrupt_btn.click(stream_interrupt, live_session, [live_status_md, live_status_json])
        live_stop_btn.click(stream_stop, live_session, [live_status_md, live_status_json])
        live_preview_timer.tick(
            stream_preview, inputs=None,
            outputs=[live_preview, live_status_md, live_status_json], queue=False)

    with gr.Tab("⚙️ Cấu hình TTS"):
        gr.Markdown(
            "### Cấu hình TTS\n"
            "Điền API key / URL / giọng cho từng provider rồi **Lưu** — ghi vào `.env` và "
            "áp dụng ngay (không cần restart). Tốc độ đọc chung mặc định là **1.2**.")
        cfg_status = gr.Markdown(tts_config.status_markdown())

        cfg_default = gr.Dropdown(
            choices=[(tts_config.PROVIDER_LABELS[n], n) for n in tts_config.PROVIDER_FIELDS],
            value=tts_config.current_default_provider(),
            label="⭐ Provider mặc định (DEFAULT_TTS_PROVIDER)")

        _cur = tts_config.current_values()
        cfg_inputs = []          # giữ ĐÚNG THỨ TỰ tts_config.ALL_KEYS để map khi lưu
        cfg_comp = {}            # key -> component, để wire nút Vbee

        with gr.Accordion("⚙️ Cấu hình chung", open=True):
            with gr.Row():
                for key, label, secret in tts_config.COMMON_FIELDS:
                    box = gr.Textbox(
                        label=label, value=_cur.get(key, "") or ("1.2" if key == "TTS_SPEED" else ""),
                        type=("password" if secret else "text"),
                        placeholder=key)
                    cfg_inputs.append(box)
                    cfg_comp[key] = box

        for prov, fields in tts_config.PROVIDER_FIELDS.items():
            with gr.Accordion(f"🔊 {tts_config.PROVIDER_LABELS[prov]}", open=(prov in ("vbee", "ausynclab"))):
                with gr.Row():
                    for key, label, secret in fields:
                        box = gr.Textbox(
                            label=label, value=_cur.get(key, ""),
                            type=("password" if secret else "text"),
                            placeholder=key)
                        cfg_inputs.append(box)
                        cfg_comp[key] = box
                # Vbee: 4 giọng cố định + nghe thử (bỏ phần kết nối/tải giọng).
                if prov == "vbee":
                    vbee_voices_dd = gr.Dropdown(
                        label="Giọng Vbee (chọn → đặt làm mặc định)",
                        choices=VbeeTTS.CURATED_VOICES,
                        value=VbeeTTS.CURATED_VOICES[0][1])
                    with gr.Row():
                        vbee_test_text = gr.Textbox(label="Nghe thử câu nói",
                                                    value="Xin chào, đây là giọng đọc thử.")
                        vbee_test_btn = gr.Button("▶ Nghe thử")
                    vbee_test_audio = gr.Audio(label="Kết quả nghe thử", type="filepath")
                    vbee_voices_dd.change(lambda v: gr.update(value=v),
                                          vbee_voices_dd, cfg_comp["VBEE_DEFAULT_VOICE"])
                    vbee_test_btn.click(vbee_test,
                                        [cfg_comp["VBEE_APP_ID"], cfg_comp["VBEE_TOKEN"],
                                         cfg_comp["VBEE_DEFAULT_VOICE"], vbee_test_text],
                                        vbee_test_audio)
                # AusyncLab: 4 giọng cố định + nghe thử (bỏ phần kết nối/tải giọng).
                if prov == "ausynclab":
                    aus_voices_dd = gr.Dropdown(
                        label="Voice AusyncLab (chọn → đặt mặc định)",
                        choices=AusynclabTTS.CURATED_VOICES,
                        value=AusynclabTTS.CURATED_VOICES[0][1])
                    with gr.Row():
                        aus_test_text = gr.Textbox(label="Nghe thử câu nói",
                                                   value="Xin chào, đây là giọng đọc thử.")
                        aus_test_btn = gr.Button("▶ Nghe thử")
                    aus_test_audio = gr.Audio(label="Kết quả nghe thử", type="filepath")
                    aus_voices_dd.change(lambda v: gr.update(value=v),
                                         aus_voices_dd, cfg_comp["AUSYNCLAB_DEFAULT_VOICE"])
                    aus_test_btn.click(ausynclab_test,
                                       [cfg_comp["AUSYNCLAB_API_KEY"],
                                        cfg_comp["AUSYNCLAB_DEFAULT_VOICE"], aus_test_text],
                                       aus_test_audio)
                # Voice he thong: co the tai danh sach voice neu endpoint ho tro, hoac nhap tay voice_name.
                if prov == "autovoice":
                    with gr.Row():
                        autovoice_load_btn = gr.Button("🔌 Kết nối & tải giọng")
                        autovoice_status = gr.Markdown()
                    autovoice_voices_dd = gr.Dropdown(
                        label="Voice hệ thống (chọn hoặc nhập voiceId)",
                        choices=[],
                        value=_cur.get("AUTOVOICE_DEFAULT_VOICE", ""),
                        allow_custom_value=True)
                    with gr.Row():
                        autovoice_test_text = gr.Textbox(label="Nghe thử câu nói",
                                                         value="Xin chào, đây là giọng đọc thử.")
                        autovoice_test_btn = gr.Button("▶ Nghe thử")
                    autovoice_test_audio = gr.Audio(label="Kết quả nghe thử", type="filepath")
                    autovoice_load_btn.click(
                        autovoice_connect,
                        [cfg_comp["AUTOVOICE_API_KEY"], cfg_comp["AUTOVOICE_URL"],
                         cfg_comp["AUTOVOICE_VOICES_URL"]],
                        [autovoice_voices_dd, autovoice_status])
                    autovoice_voices_dd.change(lambda v: gr.update(value=v),
                                               autovoice_voices_dd,
                                               cfg_comp["AUTOVOICE_DEFAULT_VOICE"])
                    autovoice_test_btn.click(
                        autovoice_test,
                        [cfg_comp["AUTOVOICE_API_KEY"], cfg_comp["AUTOVOICE_DEFAULT_VOICE"],
                         cfg_comp["AUTOVOICE_URL"], cfg_comp["AUTOVOICE_SPEED"],
                         autovoice_test_text],
                        autovoice_test_audio)
        cfg_save = gr.Button("💾 Lưu cấu hình TTS", variant="primary")
        # Lưu: ghi .env + reset factory; cập nhật bảng trạng thái + dropdown provider ở tab Import.
        cfg_save.click(save_tts_config, [cfg_default, *cfg_inputs],
                       [cfg_status, xl_default_provider, cfg_default, *cfg_inputs])

    # Khi mở/refresh trang: nạp bảng + danh sách video; khu Tab 1 tự hiện video mới nhất.
    demo.load(refresh_status, outputs=[status_table, done_dd])
    demo.load(load_video_area, outputs=[result_dd, result_video, result_dl])
    demo.load(tts_status_md, outputs=xl_tts_status)
    # Form cấu hình TTS luôn phản ánh .env đã lưu (kể cả sau khi lưu rồi mở lại trang).
    demo.load(load_tts_config_form, outputs=[cfg_default, *cfg_inputs, cfg_status])


# Gradio always injects a manifest link; enable its built-in PWA route to avoid 404.
demo.pwa = True

if __name__ == "__main__":
    demo.queue()  # cho render đồng bộ chạy tuần tự, không nghẽn server
    # allowed_paths: cho phép Gradio phục vụ video kết quả nằm ngoài thư mục app (trên Desktop).
    # Local-only by default. Public sharing must be explicitly opted into with GRADIO_SHARE=1.
    demo.launch(
        server_name=os.environ.get("GRADIO_SERVER_NAME", "127.0.0.1"),
        server_port=int(os.environ.get("GRADIO_SERVER_PORT", "7860")),
        inbrowser=False,
        share=os.environ.get("GRADIO_SHARE", "0") == "1",
        pwa=True,
        allowed_paths=[str(db.RENDERS_DIR)],
    )
