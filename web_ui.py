"""Web UI for the LatentSync render queue (Gradio 5, 2 tabs).

Tab 1 — Render: upload video + audio, full config (như bản gradio_app cũ), rồi chọn
  • "▶ Render ngay"   — render đồng bộ ngay trong tiến trình này, hiện video kết quả tại chỗ.
  • "➕ Thêm vào queue" — đẩy job (kèm toàn bộ config) cho queue_worker.py render nền.
Tab 2 — Trạng thái queue: bảng auto-refresh 10s + xem/tải video đã render.

Run with:  conda activate latentsync && python web_ui.py
"""
import re
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path

import gradio as gr

import database as db
from render_job import render, OUT_RES  # in-process render dùng cho "Render ngay"

ROOT = Path(__file__).parent
UPLOADS_DIR = ROOT / "uploads"
OUTPUT_DIR = ROOT / "output"        # nơi chứa video render-now

# Bảo đảm bảng tồn tại trước khi Blocks query giá trị khởi tạo.
db.init_db()
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

STATUS_LABEL = {
    "queued": "⏳ Chờ",
    "rendering": "🎬 Đang render",
    "done": "✅ Xong",
    "failed": "❌ Lỗi",
}


def _safe_stem(name):
    return re.sub(r"[^\w.-]+", "_", (name or "").strip()) or "job"


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


# ---------------------------------------------------------------- Render ngay (đồng bộ)

# Chỉ cho 1 "Render ngay" chạy đồng bộ tại 1 thời điểm. Bấm tiếp khi đang render -> job
# đó tự được đẩy vào SQLite queue cho worker, thay vì render đồng bộ lần nữa.
_render_now_busy = threading.Lock()


def render_now(video_path, audio_path, product, kind, intent, model_res, guidance, steps, seed,
               enhance_mouth, enhance_region, out_res):
    if not video_path or not audio_path:
        raise gr.Error("Cần cả video và audio.")
    name = build_name(product, kind, intent)   # tên chuẩn <sp>__<INTENT>

    # Đang có 1 render đồng bộ chạy -> đưa job này vào queue thay vì render ngay.
    if not _render_now_busy.acquire(blocking=False):
        qmsg = add_to_queue(video_path, audio_path, product, kind, intent, model_res, guidance, steps,
                            seed, enhance_mouth, enhance_region, out_res)
        return None, "⏳ Đang render 1 video — đã đưa job này **VÀO QUEUE** (worker sẽ render). " + qmsg

    try:
        config_path, checkpoint = db.MODELS[str(model_res)]
        # Download ra ĐÚNG TÊN chuẩn (khóa match khi import sang hệ live).
        stem = _safe_stem(name)
        out_path = OUTPUT_DIR / f"{stem}.mp4"
        if out_path.exists():
            out_path = OUTPUT_DIR / f"{stem}_{datetime.now().strftime('%H%M%S')}.mp4"
        try:
            render(video_path, audio_path, str(out_path), config_path, checkpoint,
                   float(guidance), int(steps), int(seed), bool(enhance_mouth),
                   str(enhance_region), str(out_res))
        except Exception as e:
            raise gr.Error(f"Lỗi render: {e}")
        return str(out_path), f"✅ Render xong: {out_path.name}"
    finally:
        _render_now_busy.release()


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
    return f"✅ Đã thêm **job #{job_id}** ('{name}', model {model_res}) vào queue."


# ---------------------------------------------------------------- Tab 2 helpers

def _table_rows():
    rows = []
    for j in db.list_jobs(limit=200):
        rows.append([
            j["id"], j["name"], j["model_res"],
            STATUS_LABEL.get(j["status"], j["status"]),
            j["retries"], j["created_at"] or "",
            (j["error"] or "")[:80],
        ])
    return rows


def _done_choices():
    # label -> output file path, cho dropdown xem/tải.
    return {f"#{j['id']} · {j['name']}": j["output_path"] for j in db.list_done_jobs(limit=200)}


def refresh_status():
    return (
        gr.update(value=_table_rows()),
        gr.update(choices=list(_done_choices().keys())),
    )


def pick_done(label):
    """Chọn 1 job đã xong -> trả (video preview, file tải về)."""
    path = _done_choices().get(label) if label else None
    return path, path


with gr.Blocks(title="LatentSync Render Queue") as demo:
    gr.Markdown("<h1 align='center'>LatentSync — Render Queue 24/7</h1>")

    with gr.Tab("🎬 Render / Tạo job"):
        with gr.Row():
            with gr.Column():
                video_in = gr.Video(label="Video")
                audio_in = gr.Audio(label="Audio", type="filepath")

                # Đặt tên CHUẨN để hệ live tự match video↔sản phẩm khi import.
                product_in = gr.Textbox(
                    label="Sản phẩm (tên hoặc Shopee item_id)",
                    placeholder="vd: set kep toc  hoặc  23525384022")
                with gr.Row():
                    kind_in = gr.Radio([KIND_INTRO, KIND_ANSWER], value=KIND_INTRO,
                                       label="Loại video", scale=2)
                    intent_in = gr.Dropdown(choices=INTENT_CHOICES, label="Câu hỏi (khi Trả lời)",
                                            visible=False, scale=2)
                name_preview_md = gr.Markdown(
                    "📄 Tên file xuất = **&lt;sản phẩm&gt;__&lt;INTENT&gt;.mp4** "
                    "(Giới thiệu thì bỏ phần __INTENT). Hệ live sẽ tự khớp video↔sản phẩm theo tên này.")

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
                    render_btn = gr.Button("▶ Render ngay", variant="primary")
                    queue_btn = gr.Button("➕ Thêm vào queue", variant="secondary")
                msg = gr.Markdown()

            with gr.Column():
                video_out = gr.Video(label="Video kết quả (Render ngay)")

        cfg_inputs = [video_in, audio_in, product_in, kind_in, intent_in, model_in, guidance_in,
                      steps_in, seed_in, enhance_mouth_in, region_in, out_res_in]
        # Hiện ô câu hỏi chỉ khi chọn "Trả lời".
        kind_in.change(lambda k: gr.update(visible=(k == KIND_ANSWER)), kind_in, intent_in)
        # concurrency_limit=None: cho click thứ 2 vào hàm ngay (không bị Gradio xếp tuần tự),
        # nhờ đó _render_now_busy phát hiện đang bận và đẩy job vào queue.
        render_btn.click(render_now, cfg_inputs, [video_out, msg], concurrency_limit=None)
        queue_btn.click(add_to_queue, cfg_inputs, msg)

    with gr.Tab("📊 Trạng thái queue"):
        gr.Markdown("Tự refresh mỗi 10 giây.")
        status_table = gr.Dataframe(
            headers=["ID", "Tên", "Model", "Trạng thái", "Retry", "Tạo lúc", "Lỗi"],
            datatype=["number", "str", "str", "str", "number", "str", "str"],
            value=_table_rows(), interactive=False, wrap=True,
        )
        with gr.Row():
            done_dd = gr.Dropdown(label="Job đã xong (xem / tải)",
                                  choices=list(_done_choices().keys()))
            download_file = gr.File(label="Tải về")
        done_video = gr.Video(label="Video đã render")
        done_dd.change(pick_done, done_dd, [done_video, download_file])

        timer = gr.Timer(10)
        timer.tick(refresh_status, outputs=[status_table, done_dd])

    # Refresh trạng thái queue ngay khi mở/refresh trang (đọc từ DB nên không mất khi reload).
    demo.load(refresh_status, outputs=[status_table, done_dd])


if __name__ == "__main__":
    demo.queue()  # cho render đồng bộ chạy tuần tự, không nghẽn server
    demo.launch(inbrowser=True, share=True)
