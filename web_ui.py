"""Web UI for the LatentSync render queue (Gradio 5, 2 tabs).

Tab 1 — Render: upload video + audio, full config (như bản gradio_app cũ), rồi bấm
  "▶ Render (vào hàng đợi)" / "➕ Thêm vào hàng đợi" — cả hai đều đẩy job (kèm toàn bộ config)
  cho queue_worker.py render NỀN, TUẦN TỰ (một job xong mới tới job kế -> không tranh GPU).
Tab 2 — Trạng thái queue: bảng auto-refresh 10s + xem/tải video đã render.

Run with:  conda activate latentsync && python web_ui.py
"""
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
]


def build_name(product, kind, intent):
    """Dựng tên chuẩn từ ô nhập: '<sản phẩm>__<INTENT>' (Trả lời) hoặc '<sản phẩm>' (Giới thiệu)."""
    product = (product or "").strip()
    if kind == KIND_ANSWER and intent:
        return f"{product}__{intent}" if product else f"__{intent}"
    return product or "video"


def name_preview(product, kind, intent):
    return f"📄 Tên file xuất: **{_safe_stem(build_name(product, kind, intent))}.mp4**"


# ---------------------------------------------------------------- Thêm vào queue

def add_to_queue(video_path, audio_path, product, kind, intent, model_res, guidance, steps, seed,
                 enhance_mouth, enhance_region, out_res):
    if not video_path or not audio_path:
        raise gr.Error("Cần cả video và audio.")
    name = build_name(product, kind, intent)   # tên chuẩn <sp>__<INTENT>
    job_dir = UPLOADS_DIR / uuid.uuid4().hex[:12]
    job_dir.mkdir(parents=True, exist_ok=True)
    # Copy uploads tới chỗ ổn định (file temp của Gradio bị dọn khi thoát).
    v_dst = job_dir / ("video" + Path(video_path).suffix)
    a_dst = job_dir / ("audio" + Path(audio_path).suffix)
    shutil.copy(video_path, v_dst)
    shutil.copy(audio_path, a_dst)
    job_id = db.add_job(name, v_dst, a_dst, model_res, guidance, steps, seed,
                        int(bool(enhance_mouth)), enhance_region, out_res)
    gr.Info(f"✅ Đã thêm job #{job_id} vào queue.")   # toast thông báo
    return f"✅ Đã thêm **job #{job_id}** ('{name}', model {model_res}) vào queue."


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


def _table_rows():
    rows = []
    for j in db.list_jobs(limit=200):
        rows.append([
            j["id"], j["name"], j["model_res"],
            STATUS_LABEL.get(j["status"], j["status"]),
            _job_duration(j),
            j["retries"], j["created_at"] or "",
            (j["error"] or "")[:80],
        ])
    return rows


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
        if vp.exists() and UPLOADS_DIR in vp.parents:   # uploads/<hex>/video.ext -> xóa cả thư mục tạm
            shutil.rmtree(vp.parent, ignore_errors=True)
    except Exception:
        pass


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
        out_name = excel_import.build_name_excel(r["product"], r["video_type"], r["question_type"])
        table.append([r["row"], r["product"] or "(chung)", Path(r["video_path"]).name, r["video_type"],
                      out_name + ".mp4", r["tts_provider"] or "(mặc định)", "✅ Ready"])
    for e in errors:
        table.append([e["row"], "—", "—", "—", "—", "—", f"❌ {e['error']}"])
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


def preview_excel(excel_file, default_video, default_provider, shopee_item_id):
    if not excel_file:
        raise gr.Error("Chưa chọn file Excel.")
    shopee = (shopee_item_id or "").strip() or None
    stable_default = _stabilize_video(default_video)   # copy video tải lên -> đường dẫn bền
    rows, errors = excel_import.process_excel(
        excel_file, stable_default, default_provider, shopee)
    table = _preview_rows_table(rows, errors, shopee)
    summary = (f"**Tổng: {len(rows) + len(errors)} | "
               f"Sẵn sàng: {len(rows)} | Lỗi: {len(errors)}**")
    state = {"rows": rows, "shopee": (shopee_item_id or "").strip() or None}
    gr.Info(f"Preview: {len(rows)} sẵn sàng, {len(errors)} lỗi.")
    return table, summary, state


def submit_excel(state, progress=gr.Progress()):
    if not state or not state.get("rows"):
        raise gr.Error("Chưa có dòng sẵn sàng — bấm Preview trước.")
    rows = state["rows"]
    shopee = state.get("shopee")

    def _cb(done, total, label):
        progress(done / max(1, total), desc=label)

    enqueued, warnings, batch_id, skipped = excel_import.submit_jobs(
        rows, shopee_item_id=shopee, progress=_cb)
    lines = [f"### ✅ Đã đưa **{len(enqueued)}** dòng vào TTS queue (batch `{batch_id}`).",
             "TTS worker sẽ tạo giọng (rate-limit + tự retry khi nghẽn) rồi đẩy sang hàng đợi render — "
             "không còn rớt dòng vì lỗi tạm thời. Theo dõi ở mục **Trạng thái TTS** bên dưới."]
    if skipped:
        lines.append(f"\n⏭ **Bỏ qua {len(skipped)} dòng TRÙNG** (đã có job chờ/đang chạy/đã xong — "
                     "tránh render lại cùng nội dung):")
        for s in skipped[:15]:
            lines.append(f"- dòng {s['row']}: '{s['name']}' (trùng tts#{s['dup_id']})")
        if len(skipped) > 15:
            lines.append(f"- … và {len(skipped) - 15} dòng nữa")
    if warnings:
        lines.append(f"\n⚠ **{len(warnings)} dòng được sửa voice** (voice guard):")
        for w in warnings:
            lines.append(f"- dòng {w['row']}: {w['warn']}")
    gr.Info(f"Enqueue {len(enqueued)} dòng, bỏ {len(skipped)} dòng trùng.")
    return "\n".join(lines), gr.update(value=_table_rows())


# ---------------------------------------------------------------- Tab Pipeline Excel (format mới)

def preview_excel_new(excel_file, default_video, default_provider):
    """Preview file Excel định dạng mới (sheet 'FILE IMPORT', header row 3, data từ row 5)."""
    if not excel_file:
        raise gr.Error("Chưa chọn file Excel.")
    try:
        from latentsync.excel_import import ExcelImporter
        importer = ExcelImporter(excel_file)
        rows = importer.parse()
    except Exception as e:
        raise gr.Error(f"Lỗi đọc Excel: {e}")
    table = []
    for r in rows:
        status = r.get("status") or "pending"
        if r.get("_error"):
            status = f"❌ {r['_error']}"
        table.append([
            r["_row"],
            r.get("product_name") or r.get("shopee_item_id") or "(trống)",
            r.get("video_type") or "",
            (r.get("text") or "")[:50],
            r.get("tts_provider") or f"({default_provider or 'default'})",
            status,
        ])
    ok = sum(1 for r in rows if not r.get("_error"))
    err = sum(1 for r in rows if r.get("_error"))
    summary = f"**Tổng: {len(rows)} dòng | Sẵn sàng: {ok} | Lỗi validate: {err}**"
    gr.Info(f"Preview: {ok} sẵn sàng, {err} lỗi.")
    return table, summary


def run_pipeline_ui(excel_file, default_video, default_provider, upload_drive):
    """Generator — yield log strings cập nhật Textbox realtime."""
    if not excel_file:
        yield "❌ Chưa upload file Excel."
        return
    try:
        from latentsync.excel_pipeline import ExcelPipeline
        pipeline = ExcelPipeline(
            excel_path=excel_file,
            default_video=(default_video or "").strip() or None,
            default_tts_provider=default_provider or None,
            upload_drive=bool(upload_drive),
        )
        log = ""
        for line in pipeline.run():
            log += line + "\n"
            yield log
    except Exception as e:
        yield f"❌ Pipeline lỗi: {e}"


def export_excel_ui(excel_file):
    """Trả file Excel (đã cập nhật video_done/video_url/status) để download."""
    if not excel_file or not Path(excel_file).exists():
        raise gr.Error("Chưa có file Excel nào để xuất.")
    return excel_file


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
    """Lưu cấu hình 4 provider vào .env + áp dụng ngay (reset factory). Outputs: [status_md, provider_dropdowns...]."""
    values = dict(zip(tts_config.ALL_KEYS, vals))
    providers = tts_config.save_config(values, default_provider)
    md = "### ✅ Đã lưu cấu hình TTS\n\n" + tts_config.status_markdown(providers)
    # Cập nhật lại dropdown provider mặc định ở tab Import (nhãn (chưa cấu hình) có thể đổi).
    choices = [(f"{p['label']}{'' if p['enabled'] else ' (chưa cấu hình)'}", p["name"]) for p in providers]
    gr.Info("Đã lưu .env và áp dụng cấu hình TTS.")
    return md, gr.update(choices=choices, value=default_provider)


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
"""

with gr.Blocks(title="Render Queue", css=CSS) as demo:
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
                    name_preview_md = gr.Markdown(name_preview("", KIND_INTRO, None),
                                                  elem_id="name-preview")

                with gr.Row():
                    model_in = gr.Radio(
                        choices=["256", "512"], value="256",
                        label="Model: 256 (nhanh ~2×) | 512 (nét/tự nhiên, chậm)",
                    )
                    out_res_in = gr.Radio(
                        choices=list(OUT_RES.keys()), value="720",
                        label="Độ phân giải (cạnh ngắn)",
                    )
                with gr.Row():
                    guidance_in = gr.Slider(1.0, 3.0, value=1.5, step=0.1, label="Guidance Scale")
                    steps_in = gr.Slider(8, 50, value=20, step=1, label="Inference Steps")
                with gr.Row():
                    seed_in = gr.Number(value=1247, label="Seed", precision=0)
                    enhance_mouth_in = gr.Checkbox(value=True, label="Làm nét miệng (GFPGAN)")
                    region_in = gr.Radio(["mouth", "face"], value="mouth", label="Vùng làm nét")

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

        cfg_inputs = [video_in, audio_in, product_in, kind_in, intent_in, model_in, guidance_in,
                      steps_in, seed_in, enhance_mouth_in, region_in, out_res_in]
        # Hiện ô câu hỏi chỉ khi chọn "Trả lời".
        kind_in.change(lambda k: gr.update(visible=(k == KIND_ANSWER)), kind_in, intent_in)
        # Preview tên file xuất cập nhật TRỰC TIẾP khi gõ tên / đổi loại / đổi câu hỏi.
        _name_inputs = [product_in, kind_in, intent_in]
        product_in.input(name_preview, _name_inputs, name_preview_md)
        kind_in.change(name_preview, _name_inputs, name_preview_md)
        intent_in.change(name_preview, _name_inputs, name_preview_md)
        # Cả HAI nút đều ĐẨY VÀO HÀNG ĐỢI (worker render nền, tuần tự — không còn render in-process
        # nên không tranh GPU). Báo NGAY "đang thêm" rồi mới copy file + thêm DB (kèm toast).
        render_btn.click(lambda: "⏳ Đang thêm vào hàng đợi…", None, msg).then(add_to_queue, cfg_inputs, msg)
        queue_btn.click(lambda: "⏳ Đang thêm vào hàng đợi…", None, msg).then(add_to_queue, cfg_inputs, msg)
        # Khu xem lại: chọn video -> phát + cho tải; nút 🔄 cập nhật danh sách + hiện video mới nhất.
        result_dd.input(pick_done, result_dd, [result_video, result_dl])
        result_refresh.click(load_video_area, None, [result_dd, result_video, result_dl])

    with gr.Tab("📊 Trạng thái queue"):
        gr.Markdown("Tự refresh mỗi 10 giây.")
        status_table = gr.Dataframe(
            headers=["ID", "Tên", "Model", "Trạng thái", "⏱ Thời gian render", "Retry", "Tạo lúc", "Lỗi"],
            datatype=["number", "str", "str", "str", "str", "number", "str", "str"],
            value=_table_rows(), interactive=False, wrap=True,
        )
        with gr.Row():
            done_dd = gr.Dropdown(label="Video đã render (xem / tải)",
                                  choices=list(_video_choices().keys()))
            download_file = gr.File(label="Tải về")
        done_video = gr.Video(label="Video đã render")
        # .input (not .change): chỉ kích hoạt khi NGƯỜI DÙNG chọn, tránh trigger rỗng
        # khi timer/load cập nhật lại choices (gây lỗi "got: 0").
        done_dd.input(pick_done, done_dd, [done_video, download_file])

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
        # Cập nhật danh sách video ở khu xem lại Tab 1 (chỉ choices -> không cắt ngang video đang phát).
        timer.tick(lambda: gr.update(choices=list(_video_choices().keys())), None, result_dd)

    with gr.Tab("📥 Import Excel"):
        gr.Markdown(
            "### Import hàng loạt từ Excel → TTS đa luồng → hàng đợi render\n"
            "Mỗi dòng = 1 job. TTS chạy **song song**; render vẫn **tuần tự** (1 GPU). "
            "Job dùng cấu hình mặc định **model 256 + làm nét miệng**.")

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

        gr.Markdown("#### Import")
        xl_file = gr.File(label="Upload file Excel (.xlsx)", file_types=[".xlsx"])
        with gr.Row():
            xl_preview_btn = gr.Button("🔍 Preview", variant="secondary")
            xl_submit_btn = gr.Button("🚀 Submit các job sẵn sàng", variant="primary")
        xl_summary = gr.Markdown()
        xl_table = gr.Dataframe(
            headers=["Row", "Sản phẩm", "Video", "Type", "Tên xuất (.mp4)", "Provider", "Status"],
            datatype=["number", "str", "str", "str", "str", "str", "str"],
            interactive=False, wrap=True)
        xl_state = gr.State()
        xl_result = gr.Markdown()

        gr.Markdown("#### 📨 Trạng thái TTS queue (tự cập nhật 10s)")
        xl_tts_status = gr.Markdown()
        xl_tts_requeue = gr.Button("🔁 Chạy lại dead-letter (các dòng lỗi tạm thời)")

        xl_preview_btn.click(
            preview_excel,
            [xl_file, xl_default_video, xl_default_provider, xl_shopee],
            [xl_table, xl_summary, xl_state])
        # Submit: enqueue vào TTS queue (worker tự synth + đẩy render), refresh bảng trạng thái.
        xl_submit_btn.click(submit_excel, xl_state, [xl_result, status_table])
        xl_tts_requeue.click(requeue_dead_letter, None, xl_tts_status)
        timer.tick(tts_status_md, outputs=xl_tts_status)   # 'timer' định nghĩa ở tab Trạng thái queue

    with gr.Tab("📦 Pipeline Excel"):
        gr.Markdown(
            "### Pipeline Excel → TTS → Render → Google Drive → Excel\n"
            "Định dạng: sheet **FILE IMPORT**, header **row 3**, data từ **row 5**.\n"
            "Chạy tuần tự từng dòng (TTS inline → đợi render xong → Drive) và ghi kết quả "
            "ngược vào Excel (video_done, video_url, status, render_time_s).")

        gr.Markdown("#### Cấu hình mặc định")
        with gr.Row():
            pip_default_video = gr.Textbox(
                label="Video mặc định (đường dẫn local, cho dòng trống cột video_path)",
                placeholder="/home/ubuntu/videos/avatar.mp4", scale=2)
            pip_default_provider = gr.Dropdown(
                choices=TTS_PROVIDER_CHOICES, value=_TTS_DEFAULT,
                label="TTS Provider mặc định", scale=1)
            pip_default_voice = gr.Dropdown(
                choices=list_voices(_TTS_DEFAULT), value=None,
                label="Giọng mặc định", allow_custom_value=True, scale=1)
            pip_upload_drive = gr.Checkbox(
                value=False, label="☁️ Upload Google Drive sau render", scale=1)
        pip_default_provider.change(update_voice_choices, pip_default_provider, pip_default_voice)

        gr.Markdown("#### Import")
        pip_file = gr.File(label="Upload file Excel (.xlsx)", file_types=[".xlsx"])
        with gr.Row():
            pip_preview_btn = gr.Button("🔍 Preview", variant="secondary")
            pip_import_btn  = gr.Button("🚀 Bắt đầu Import", variant="primary")
            pip_export_btn  = gr.Button("📤 Xuất Excel đã cập nhật", variant="secondary")
        pip_summary = gr.Markdown()
        pip_table = gr.Dataframe(
            headers=["Row", "Sản phẩm", "Type", "Text (50 ký tự)", "Provider", "Status"],
            datatype=["number", "str", "str", "str", "str", "str"],
            interactive=False, wrap=True)
        pip_progress = gr.Textbox(
            label="📋 Log realtime", lines=18, interactive=False, show_copy_button=True)
        pip_export_file = gr.File(label="File Excel đã cập nhật (download)")

        pip_preview_btn.click(
            preview_excel_new,
            [pip_file, pip_default_video, pip_default_provider],
            [pip_table, pip_summary])
        pip_import_btn.click(
            run_pipeline_ui,
            [pip_file, pip_default_video, pip_default_provider, pip_upload_drive],
            pip_progress)
        pip_export_btn.click(export_excel_ui, pip_file, pip_export_file)

    with gr.Tab("⚙️ Cấu hình TTS"):
        gr.Markdown(
            "### Cấu hình 4 loại TTS\n"
            "Điền API key / URL / giọng cho từng provider rồi **Lưu** — ghi vào `.env` và "
            "áp dụng ngay (không cần restart). Provider 'local' chạy offline, không cần key.")
        cfg_status = gr.Markdown(tts_config.status_markdown())

        cfg_default = gr.Dropdown(
            choices=[(tts_config.PROVIDER_LABELS[n], n) for n in tts_config.PROVIDER_FIELDS],
            value=tts_config.current_default_provider(),
            label="⭐ Provider mặc định (DEFAULT_TTS_PROVIDER)")

        _cur = tts_config.current_values()
        cfg_inputs = []          # giữ ĐÚNG THỨ TỰ tts_config.ALL_KEYS để map khi lưu
        cfg_comp = {}            # key -> component, để wire nút Vbee
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
        cfg_save = gr.Button("💾 Lưu cấu hình TTS", variant="primary")
        # Lưu: ghi .env + reset factory; cập nhật bảng trạng thái + dropdown provider ở tab Import.
        cfg_save.click(save_tts_config, [cfg_default, *cfg_inputs],
                       [cfg_status, xl_default_provider])

    # Khi mở/refresh trang: nạp bảng + danh sách video; khu Tab 1 tự hiện video mới nhất.
    demo.load(refresh_status, outputs=[status_table, done_dd])
    demo.load(load_video_area, outputs=[result_dd, result_video, result_dl])
    demo.load(tts_status_md, outputs=xl_tts_status)
    # Trạng thái cấu hình TTS luôn phản ánh .env đã lưu (kể cả sau khi lưu rồi mở lại trang).
    demo.load(lambda: tts_config.status_markdown(), outputs=cfg_status)


if __name__ == "__main__":
    demo.queue()  # cho render đồng bộ chạy tuần tự, không nghẽn server
    # allowed_paths: cho phép Gradio phục vụ video kết quả nằm ngoài thư mục app (trên Desktop).
    demo.launch(inbrowser=True, share=True, allowed_paths=[str(db.RENDERS_DIR)])
