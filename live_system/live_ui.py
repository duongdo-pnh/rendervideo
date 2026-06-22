"""Gradio UI cho hệ thống live. create_ui(controller) -> gr.Blocks.

4 tab: Sản phẩm (form đầy đủ + sửa + import) | Video (thư viện + sửa + import) |
Playlist | Live Control (auto refresh 5s).
"""
import shutil
import subprocess
import uuid
from pathlib import Path

import gradio as gr

import live_database as db
import live_import

ROOT = Path(__file__).parent.parent
DOWNLOADS_DIR = ROOT / "downloads"
IMAGES_DIR = Path(__file__).parent / "product_images"
TEMPLATES_DIR = Path(__file__).parent / "templates"
IMAGES_DIR.mkdir(parents=True, exist_ok=True)


def _duration(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float(out.stdout.strip())
    except Exception:
        return 0.0


def _parse_id(label):
    if not label:
        return None
    try:
        return int(str(label).split("·")[0].strip().lstrip("#"))
    except Exception:
        return None


def _save_image(image_file):
    if not image_file:
        return None
    dst = IMAGES_DIR / f"{uuid.uuid4().hex[:10]}{Path(image_file).suffix}"
    shutil.copy(image_file, dst)
    return str(dst)


# ============================================================ PRODUCTS

PRODUCT_HEADERS = ["ID", "Tên", "Giá", "Tồn kho", "Ảnh"]


def _product_rows():
    return [[p.id, p.name, p.price or 0, p.stock or 0,
             Path(p.image_path).name if p.image_path else ""]
            for p in db.get_all_products()]


def _product_choices():
    return [f"#{p.id} · {p.name}" for p in db.get_all_products()]


def _after_product_change():
    """Refresh: bảng SP + dropdown chọn SP (tab SP) + dropdown ghép SP (tab Video)."""
    return (_product_rows(), gr.update(choices=_product_choices()),
            gr.update(choices=_product_choices()))


def add_product_ui(name, sku, link, price, sale_price, commission, stock, group, pin_order,
                   desc, script, image_file):
    if not name or not str(name).strip():
        raise gr.Error("Cần nhập tên sản phẩm.")
    db.add_product(str(name).strip(), price, sale_price, _save_image(image_file),
                   sku=(sku or None), link=(link or None), commission=(commission or 0),
                   stock=int(stock or 0), description=(desc or None), script=(script or None),
                   group_name=(group or None), pin_order=int(pin_order or 0))
    return _after_product_change()


def load_product_ui(label):
    pid = _parse_id(label)
    p = db.get_product(pid) if pid else None
    if not p:
        return [None, "", "", "", None, None, 0, 0, "", 0, "", "", None]
    return [pid, p.name, p.sku or "", p.link or "", p.price, p.sale_price, p.commission,
            p.stock, p.group_name or "", p.pin_order, p.description or "", p.script or "", None]


def update_product_ui(label, name, sku, link, price, sale_price, commission, stock, group,
                      pin_order, desc, script, image_file):
    pid = _parse_id(label)   # đọc id trực tiếp từ dropdown (không phụ thuộc gr.State)
    if not pid:
        gr.Warning("Chọn 1 sản phẩm ở dropdown 'Chọn SP để sửa' trước khi Cập nhật "
                   "(hoặc dùng '➕ Thêm mới' nếu muốn tạo SP mới).")
        return _after_product_change()
    fields = dict(name=str(name).strip(), sku=(sku or None), link=(link or None), price=price,
                  sale_price=sale_price, commission=(commission or 0), stock=int(stock or 0),
                  group_name=(group or None), pin_order=int(pin_order or 0),
                  description=(desc or None), script=(script or None))
    img = _save_image(image_file)
    if img:
        fields["image_path"] = img
    db.update_product(pid, **fields)
    return _after_product_change()


def delete_product_ui(label):
    pid = _parse_id(label)
    if pid is not None:
        db.delete_product(pid)
    return _after_product_change()


def import_products_ui(file):
    if not file:
        raise gr.Error("Chọn file CSV/XLSX sản phẩm.")
    a, u, e = live_import.import_products(file)
    return (*_after_product_change(), f"✅ Import sản phẩm: thêm **{a}**, cập nhật **{u}**, lỗi **{e}**")


# ============================================================ VIDEOS

VIDEO_HEADERS = ["ID", "Tên", "File", "Sản phẩm", "Nhóm", "Ưu tiên", "Giới hạn", "Đã phát", "Lỗi"]


def _scan_videos():
    if not DOWNLOADS_DIR.exists():
        return []
    return sorted(str(p) for p in DOWNLOADS_DIR.glob("*.mp4"))


def _downloads_choices():
    return [Path(p).name for p in _scan_videos()]


def _video_rows():
    return [[v.id, v.name or Path(v.file_path).name, Path(v.file_path).name, v.product_name or "",
             v.group_name or "", v.priority, v.play_limit, v.play_count, "⚠️" if v.is_error else ""]
            for v in db.get_all_videos()]


def _video_choices():
    return [f"#{v.id} · {v.name or Path(v.file_path).name}" for v in db.get_all_videos()]


def _after_video_change():
    """Refresh: bảng video + dropdown chọn video (tab Video) + dropdown thêm vào playlist."""
    return (_video_rows(), gr.update(choices=_video_choices()),
            gr.update(choices=_video_choices()))


def add_video_lib_ui(video_name, product_label):
    if not video_name:
        raise gr.Error("Chọn 1 video từ downloads/.")
    matches = [p for p in _scan_videos() if Path(p).name == video_name]
    if not matches:
        raise gr.Error("Không tìm thấy file (thử quét lại).")
    vpath = matches[0]
    db.add_video(vpath, _parse_id(product_label), _duration(vpath))
    return _after_video_change()


def load_video_ui(label):
    vid = _parse_id(label)
    v = db.get_video(vid) if vid else None
    if not v:
        return [None, "", "", None, 0, 0, False]
    plabel = None
    if v.product_id:
        p = db.get_product(v.product_id)
        if p:
            plabel = f"#{p.id} · {p.name}" + (f" [{p.sku}]" if p.sku else "")
    return [vid, v.name or "", v.group_name or "", plabel, v.priority, v.play_limit, bool(v.is_error)]


def update_video_ui(label, name, group, product_label, priority, play_limit, is_error):
    vid = _parse_id(label)   # đọc id trực tiếp từ dropdown (không phụ thuộc gr.State)
    if not vid:
        gr.Warning("Chọn 1 video ở dropdown 'Chọn video' trước khi Cập nhật.")
        return _after_video_change()
    db.update_video(vid, name=(name or None), group_name=(group or None),
                    product_id=_parse_id(product_label), priority=int(priority or 0),
                    play_limit=int(play_limit or 0), is_error=1 if is_error else 0)
    return _after_video_change()


def delete_video_ui(label):
    vid = _parse_id(label)
    if vid is not None:
        db.delete_video(vid)
    return _after_video_change()


def import_videos_ui(file):
    if not file:
        raise gr.Error("Chọn file CSV/XLSX video.")
    a, e = live_import.import_videos(file)
    return (*_after_video_change(), f"✅ Import video: thêm **{a}**, lỗi **{e}**")


def rescan_downloads_ui():
    return gr.update(choices=_downloads_choices())


# ============================================================ PLAYLIST

def _playlist_rows():
    rows = []
    for i, e in enumerate(db.get_playlist(), 1):
        rows.append([e.playlist_id, i, Path(e.file_path).name,
                     e.product_name or "(không)", "✅" if e.is_played else ""])
    return rows


def _playlist_choices():
    return [f"#{e.playlist_id} · {Path(e.file_path).name}" for e in db.get_playlist()]


def add_to_playlist_ui(video_label):
    vid = _parse_id(video_label)
    if not vid:
        raise gr.Error("Chọn 1 video trong thư viện.")
    db.add_to_playlist(vid)
    return _playlist_rows(), gr.update(choices=_playlist_choices())


def remove_playlist_ui(label):
    plid = _parse_id(label)
    if plid is not None:
        db.remove_from_playlist(plid)
    return _playlist_rows(), gr.update(choices=_playlist_choices(), value=None)


def reset_playlist_ui():
    db.clear_playlist()
    return _playlist_rows(), gr.update(choices=[], value=None)


# ============================================================ LIVE CONTROL

def _status_views(controller):
    s = controller.get_status()
    obs = "🟢 connected ✅" if s["obs_connected"] else "🔴 disconnected ❌"
    stream = "LIVE 🔴" if s["streaming"] else "Stopped ⚫"
    nv = s["current_video"] or "—"
    npd = s["current_product"] or "—"
    logs = "\n".join(s["logs"][-20:]) or "(chưa có log)"
    return obs, stream, nv, npd, logs


# ============================================================ BUILD

def create_ui(controller):
    db.init_db()
    p_tpl, v_tpl = live_import.write_templates(TEMPLATES_DIR)

    with gr.Blocks(title="Relive Studio") as demo:
        gr.Markdown("<h1 align='center'>Relive Studio — Điều khiển Live 24/7</h1>")

        # ----------------------------------------------- Tab Sản phẩm
        with gr.Tab("🛍️ Sản phẩm"):
            with gr.Row():
                with gr.Column(scale=1):
                    sel_pid = gr.State(None)
                    p_select = gr.Dropdown(label="Chọn SP để sửa (để trống = thêm mới)",
                                           choices=_product_choices())
                    p_name = gr.Textbox(label="Tên sản phẩm")
                    with gr.Row():
                        p_sku = gr.Textbox(label="SKU")
                        p_group = gr.Textbox(label="Nhóm")
                    p_link = gr.Textbox(label="Link sản phẩm")
                    with gr.Row():
                        p_price = gr.Number(label="Giá gốc")
                        p_sale = gr.Number(label="Giá KM")
                    with gr.Row():
                        p_comm = gr.Number(label="Hoa hồng %")
                        p_stock = gr.Number(label="Tồn kho", precision=0)
                        p_pin = gr.Number(label="Thứ tự ghim", precision=0)
                    p_desc = gr.Textbox(label="Mô tả ngắn", lines=2)
                    p_script = gr.Textbox(label="Script giới thiệu", lines=3)
                    p_image = gr.File(label="Ảnh sản phẩm", type="filepath", file_types=["image"])
                    with gr.Row():
                        p_add = gr.Button("➕ Thêm mới", variant="primary")
                        p_update = gr.Button("💾 Cập nhật")
                        p_del = gr.Button("🗑 Xóa", variant="stop")
                with gr.Column(scale=2):
                    p_table = gr.Dataframe(headers=PRODUCT_HEADERS, value=_product_rows(),
                                           interactive=False, wrap=True)
                    gr.Markdown("**Import hàng loạt** (CSV/Excel — cột song ngữ; đồng bộ theo SKU)")
                    with gr.Row():
                        p_import_file = gr.File(label="File sản phẩm (.csv/.xlsx)",
                                                type="filepath", file_types=[".csv", ".xlsx", ".xls"])
                        p_import_btn = gr.Button("📥 Import sản phẩm", variant="primary")
                    p_import_msg = gr.Markdown()
                    gr.File(label="⬇ Template sản phẩm", value=p_tpl)

        # ----------------------------------------------- Tab Video
        with gr.Tab("🎬 Video"):
            with gr.Row():
                with gr.Column(scale=1):
                    sel_vid = gr.State(None)
                    gr.Markdown("**Thêm video từ downloads/**")
                    with gr.Row():
                        v_dl = gr.Dropdown(label="Video trong downloads/", choices=_downloads_choices())
                        v_rescan = gr.Button("⟳")
                    v_dl_product = gr.Dropdown(label="Ghép sản phẩm", choices=_product_choices())
                    v_add = gr.Button("➕ Thêm vào thư viện", variant="primary")
                    gr.Markdown("---\n**Sửa video** (chọn ở dropdown)")
                    v_select = gr.Dropdown(label="Chọn video", choices=_video_choices())
                    v_name = gr.Textbox(label="Tên video")
                    v_group = gr.Textbox(label="Nhóm")
                    v_product = gr.Dropdown(label="Sản phẩm", choices=_product_choices())
                    with gr.Row():
                        v_priority = gr.Number(label="Ưu tiên", precision=0)
                        v_limit = gr.Number(label="Giới hạn phát (0=∞)", precision=0)
                    v_error = gr.Checkbox(label="Đánh dấu video lỗi (bỏ qua khi phát)")
                    with gr.Row():
                        v_update = gr.Button("💾 Cập nhật", variant="primary")
                        v_del = gr.Button("🗑 Xóa", variant="stop")
                with gr.Column(scale=2):
                    v_table = gr.Dataframe(headers=VIDEO_HEADERS, value=_video_rows(),
                                           interactive=False, wrap=True)
                    gr.Markdown("**Import video hàng loạt** (CSV/Excel — link SP theo SKU/tên)")
                    with gr.Row():
                        v_import_file = gr.File(label="File video (.csv/.xlsx)", type="filepath",
                                                file_types=[".csv", ".xlsx", ".xls"])
                        v_import_btn = gr.Button("📥 Import video", variant="primary")
                    v_import_msg = gr.Markdown()
                    gr.File(label="⬇ Template video", value=v_tpl)

        # ----------------------------------------------- Tab Playlist
        with gr.Tab("🎞️ Playlist"):
            with gr.Row():
                pl_video = gr.Dropdown(label="Chọn video (thư viện)", choices=_video_choices())
                pl_add = gr.Button("➕ Thêm vào playlist", variant="primary")
            pl_table = gr.Dataframe(headers=["PL_ID", "STT", "Video", "Sản phẩm", "Đã phát"],
                                    value=_playlist_rows(), interactive=False, wrap=True)
            with gr.Row():
                pl_del_dd = gr.Dropdown(label="Chọn entry để xóa", choices=_playlist_choices())
                pl_del_btn = gr.Button("Xóa khỏi playlist", variant="stop")
                pl_reset = gr.Button("Reset toàn bộ playlist", variant="stop")

        # ----------------------------------------------- Tab Live Control
        with gr.Tab("📡 Live Control"):
            obs0, stream0, nv0, np0, logs0 = _status_views(controller)
            with gr.Row():
                obs_md = gr.Markdown(f"**OBS:** {obs0}")
                stream_md = gr.Markdown(f"**Stream:** {stream0}")
            with gr.Row():
                now_video = gr.Markdown(f"**Đang phát:** {nv0}")
                now_product = gr.Markdown(f"**Sản phẩm:** {np0}")
            with gr.Row():
                btn_start = gr.Button("▶ Start Stream", variant="primary")
                btn_stop = gr.Button("⏹ Stop Stream", variant="stop")
                btn_next = gr.Button("⏭ Next Video")
            log_box = gr.Textbox(label="Log (20 dòng gần nhất)", value=logs0, lines=20,
                                 interactive=False)

        # =================================================== WIRING
        prod_form = [p_name, p_sku, p_link, p_price, p_sale, p_comm, p_stock, p_group,
                     p_pin, p_desc, p_script, p_image]
        prod_refresh = [p_table, p_select, v_dl_product]   # +v_product handled separately below

        # Sản phẩm
        p_add.click(add_product_ui, prod_form, prod_refresh)
        p_select.change(load_product_ui, p_select,
                        [sel_pid, p_name, p_sku, p_link, p_price, p_sale, p_comm, p_stock,
                         p_group, p_pin, p_desc, p_script, p_image])
        p_update.click(update_product_ui, [p_select, *prod_form], prod_refresh)
        p_del.click(delete_product_ui, p_select, prod_refresh)
        p_import_btn.click(import_products_ui, p_import_file,
                           [p_table, p_select, v_dl_product, p_import_msg])
        # đồng bộ thêm dropdown "Sản phẩm" ở tab Video sau mỗi thay đổi SP
        for ev in (p_add, p_update, p_del):
            ev.click(lambda: gr.update(choices=_product_choices()), None, v_product)
        p_import_btn.click(lambda: gr.update(choices=_product_choices()), None, v_product)

        # Video
        vid_refresh = [v_table, v_select, pl_video]
        v_rescan.click(rescan_downloads_ui, None, v_dl)
        v_add.click(add_video_lib_ui, [v_dl, v_dl_product], vid_refresh)
        v_select.change(load_video_ui, v_select,
                        [sel_vid, v_name, v_group, v_product, v_priority, v_limit, v_error])
        v_update.click(update_video_ui,
                       [v_select, v_name, v_group, v_product, v_priority, v_limit, v_error], vid_refresh)
        v_del.click(delete_video_ui, v_select, vid_refresh)
        v_import_btn.click(import_videos_ui, v_import_file,
                           [v_table, v_select, pl_video, v_import_msg])

        # Playlist
        pl_add.click(add_to_playlist_ui, pl_video, [pl_table, pl_del_dd])
        pl_del_btn.click(remove_playlist_ui, pl_del_dd, [pl_table, pl_del_dd])
        pl_reset.click(reset_playlist_ui, None, [pl_table, pl_del_dd])

        # Live Control + auto refresh 5s
        status_outputs = [obs_md, stream_md, now_video, now_product, log_box]

        def _refresh():
            obs, stream, nv, npd, logs = _status_views(controller)
            return (f"**OBS:** {obs}", f"**Stream:** {stream}",
                    f"**Đang phát:** {nv}", f"**Sản phẩm:** {npd}", logs)

        btn_start.click(lambda: controller.start_stream(), None, None).then(_refresh, None, status_outputs)
        btn_stop.click(lambda: controller.stop_stream(), None, None).then(_refresh, None, status_outputs)
        btn_next.click(lambda: controller.next_video(), None, None).then(_refresh, None, status_outputs)

        gr.Timer(5).tick(_refresh, None, status_outputs)
        demo.load(_refresh, None, status_outputs)

    return demo
