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
from render_job import OUT_RES  # bảng độ phân giải; render THỰC do queue_worker chạy nền

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

    # Khi mở/refresh trang: nạp bảng + danh sách video; khu Tab 1 tự hiện video mới nhất.
    demo.load(refresh_status, outputs=[status_table, done_dd])
    demo.load(load_video_area, outputs=[result_dd, result_video, result_dl])


if __name__ == "__main__":
    demo.queue()  # cho render đồng bộ chạy tuần tự, không nghẽn server
    # allowed_paths: cho phép Gradio phục vụ video kết quả nằm ngoài thư mục app (trên Desktop).
    demo.launch(inbrowser=True, share=True, allowed_paths=[str(db.RENDERS_DIR)])
