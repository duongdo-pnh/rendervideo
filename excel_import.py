"""Excel batch import -> TTS (multi-threaded) -> LatentSync render queue.

Flow:
  1. process_excel(): read .xlsx, validate each row -> (ready_rows, errors). NO side effects.
  2. submit_jobs(): synthesize audio for every ready row IN PARALLEL (ThreadPoolExecutor —
     TTS is I/O-bound network calls), then enqueue each as a render job. The actual video
     render stays SEQUENTIAL (single GPU) — queue_worker.py already serializes that.

Lives at repo ROOT (not inside the latentsync package) so it can `import database as db`
exactly like web_ui.py / queue_worker.py do.

Render config for Excel jobs is fixed to the UI default: model 256 + GFPGAN (mouth).
Output naming keeps the existing convention "<base>__<INTENT>" so Relive Studio still
matches video <-> product by name.
"""
import os
import re
import unicodedata
import uuid
from pathlib import Path

import pandas as pd

import database as db
import tts_db
from tts_voice import normalize_voice
from latentsync.tts.factory import DEFAULT_PROVIDER

ROOT = Path(__file__).parent
TTS_AUDIO_DIR = ROOT / "uploads" / "tts"   # stable dir (uploads/ persists; pipeline temp gets wiped)


COLUMNS = ["product", "video_path", "video_type", "question_type", "text", "tts_provider", "tts_voice"]

# Intent codes the downstream/Relive system matches on — MUST stay in sync with
# INTENT_CHOICES in web_ui.py ("Mã PHẢI trùng intents bên hệ live (ASK_*)").
# video_type maps to one of these; "gioi_thieu"/intro -> None (video giới thiệu, không __INTENT).
INTENT_MAP = {
    "gioi_thieu": None, "gioithieu": None, "intro": None, "gt": None,
    "gia": "ASK_PRICE", "price": "ASK_PRICE", "hoi_gia": "ASK_PRICE",
    "chat_luong": "ASK_QUALITY", "chatluong": "ASK_QUALITY", "quality": "ASK_QUALITY", "review": "ASK_QUALITY",
    "cach_dung": "ASK_USAGE", "usage": "ASK_USAGE", "huong_dan": "ASK_USAGE",
    "con_hang": "ASK_STOCK", "stock": "ASK_STOCK", "size": "ASK_STOCK", "mau": "ASK_STOCK",
    "mua": "ASK_BUY", "buy": "ASK_BUY", "chot_don": "ASK_BUY", "order": "ASK_BUY",
    "ship": "ASK_SHIPPING", "shipping": "ASK_SHIPPING", "giao_hang": "ASK_SHIPPING", "freeship": "ASK_SHIPPING",
    "voucher": "ASK_VOUCHER", "giam_gia": "ASK_VOUCHER", "khuyen_mai": "ASK_VOUCHER", "discount": "ASK_VOUCHER",
    "doi_tra": "ASK_RETURN", "return": "ASK_RETURN", "bao_hanh": "ASK_RETURN", "warranty": "ASK_RETURN",
    "san_pham": "ASK_PRODUCT", "product": "ASK_PRODUCT", "xem_sp": "ASK_PRODUCT",
}


def _ascii(s):
    """Bỏ dấu + lower, để nhận cả 'Giới thiệu' / 'gioi thieu' / 'Trả lời câu hỏi'.

    Thay đ/Đ -> d/D trước (NFKD KHÔNG tách 'đ' nên nếu không xử lý sẽ mất, vd 'đổi trả' -> 'oi tra')."""
    import unicodedata as _u
    s = (s or "").replace("đ", "d").replace("Đ", "D")
    return _u.normalize("NFKD", s).encode("ascii", "ignore").decode().strip().lower()


def is_intro(video_type, question_type=""):
    """video_type chỉ 2 loại: giới thiệu vs trả lời câu hỏi. True = video giới thiệu."""
    a = _ascii(video_type)
    if "tra" in a and "loi" in a:        # trả lời (câu hỏi)
        return False
    if "gioi" in a and "thieu" in a:     # giới thiệu
        return True
    if a in ("answer", "tl", "tlch", "cau_hoi", "cauhoi"):
        return False
    if a in ("intro", "gt", "introduce", ""):
        return True
    # Không rõ: có loại câu hỏi -> coi là trả lời; ngược lại -> giới thiệu.
    return not bool((question_type or "").strip())


def resolve_intent(question_type):
    """Loại câu hỏi -> mã ASK_* hệ thống (khớp INTENT_CHOICES web_ui). None nếu không xác định.

    Chấp nhận: gõ thẳng 'ASK_*', token tiếng Việt/Anh, hoặc nhãn 'Hỏi giá'... (đã bỏ dấu).
    """
    key = _ascii(question_type).replace(" ", "_").replace("-", "_")
    if not key:
        return None
    if key.upper().startswith("ASK_"):
        return key.upper()
    return INTENT_MAP.get(key)


# Per-column guidance written as cell comments in the downloadable template.
_COL_HELP = {
    "product": ("TÊN SẢN PHẨM / TÊN VIDEO — đặt tên file xuất (Relive match theo tên này). "
                "Shopee item_id hoặc tên sản phẩm. Để TRỐNG = CÂU TRẢ LỜI CHUNG (tên '__ASK_*', "
                "không gắn sản phẩm) — hoặc dùng ô 'Shopee Item ID' chung nếu có điền."),
    "video_path": "Đường dẫn local tới video avatar. Để TRỐNG = dùng video mặc định.",
    "video_type": ("CHỈ 2 giá trị: 'gioi_thieu' (video giới thiệu, tên = <sản phẩm>) "
                   "hoặc 'tra_loi' (trả lời câu hỏi, tên = <sản phẩm>__ASK_*)."),
    "question_type": ("Loại câu hỏi — CHỈ điền khi video_type = tra_loi. Nhận tiếng Việt "
                      "(gia/chat_luong/cach_dung/con_hang/mua/ship/voucher/doi_tra/san_pham) "
                      "hoặc mã ASK_* trực tiếp. Khi giới thiệu thì để TRỐNG."),
    "text": "Script cần chuyển thành giọng nói (TTS). BẮT BUỘC.",
    "tts_provider": "vbee / ausynclab / api / local. Để TRỐNG = provider mặc định.",
    "tts_voice": "Tên giọng theo provider. Để TRỐNG = giọng mặc định.",
}

# Default render config for jobs created from Excel (matches the web UI defaults).
RENDER_DEFAULTS = dict(model_res="256", guidance=1.5, steps=20, seed=1247,
                       enhance_mouth=1, enhance_region="mouth", out_res="720")

IMPORT_LOG = ROOT / "logs" / "import_tts.log"


def _log_import_error(row, msg):
    """Ghi lỗi TTS lúc import ra file để kiểm tra sau (UI chỉ hiện tạm thời)."""
    try:
        from datetime import datetime
        IMPORT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(IMPORT_LOG, "a") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] dòng {row}: {msg}\n")
    except Exception:
        pass


# ----------------------------------------------------------------- template

def make_template(path):
    """Write an .xlsx template: header + 3 example rows + per-header comments."""
    from openpyxl import Workbook
    from openpyxl.comments import Comment

    wb = Workbook()
    ws = wb.active
    ws.title = "import"
    ws.append(COLUMNS)
    # product: tên/ID sản phẩm (đặt tên video). video_type: gioi_thieu (tên=<sp>) | tra_loi (<sp>__ASK_*)
    examples = [
        ["23525384022", "/videos/avatar.mp4", "gioi_thieu", "",     "Chào cả nhà, hôm nay shop có sản phẩm cực hot!", "vbee",      ""],
        ["23525384022", "/videos/avatar.mp4", "tra_loi",    "gia",  "Dạ giá chỉ 299k ạ, đang sale cực mạnh!",         "vbee",      ""],
        ["set kep toc", "",                   "tra_loi",    "mua",  "Bấm giỏ hàng chốt đơn ngay đi ạ!",               "local",     ""],
        ["ao thun nam", "/videos/avatar2.mp4","tra_loi",    "ship", "Bên em freeship toàn quốc nha!",                 "ausynclab", ""],
    ]
    for r in examples:
        ws.append(r)
    for idx, col in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=idx)
        cell.comment = Comment(_COL_HELP[col], "LatentSync")
        ws.column_dimensions[cell.column_letter].width = max(16, len(col) + 4)
    wb.save(path)
    return path


# ----------------------------------------------------------------- naming

def _safe_stem(name):
    name = unicodedata.normalize("NFC", (name or "").strip())
    return re.sub(r"[^\w.-]+", "_", name) or "job"


def build_name_excel(product, video_type, question_type):
    """Khớp 100% web_ui.build_name(): '<sản phẩm>__<ASK_*>' / '<sản phẩm>' / (sản phẩm rỗng) '__<ASK_*>'.

    product rỗng + trả lời -> '__ASK_*' = CÂU TRẢ LỜI CHUNG (không gắn sản phẩm cụ thể).
    product rỗng + giới thiệu -> 'video'.
    """
    base = str(product).strip() if product else ""
    if is_intro(video_type, question_type):
        return base or "video"
    intent = resolve_intent(question_type)
    if not intent:
        return base or "video"
    return f"{base}__{intent}" if base else f"__{intent}"


# ----------------------------------------------------------------- read + validate

def _clean(val):
    """NaN / whitespace-only -> None; else stripped str."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    s = str(val).strip()
    return s or None


def process_excel(excel_path, default_video=None, default_provider=None, shopee_item_id=None):
    """Read + validate. Returns (ready_rows, errors). No TTS, no DB writes."""
    df = pd.read_excel(excel_path)
    rows, errors = [], []

    for i, raw in df.iterrows():
        excel_row = i + 2  # 1-based + header
        try:
            video = _clean(raw.get("video_path")) or default_video
            if not video or not os.path.exists(video):
                raise ValueError(f"Video không tồn tại: {video!r}")

            text = _clean(raw.get("text"))
            if not text:
                raise ValueError("Text rỗng")

            provider = _clean(raw.get("tts_provider")) or default_provider  # None -> factory default
            voice = _clean(raw.get("tts_voice"))                            # None -> provider default
            video_type = _clean(raw.get("video_type")) or "gioi_thieu"
            question_type = _clean(raw.get("question_type")) or ""

            # Tên sản phẩm/video: ưu tiên cột 'product' của dòng, else ô Shopee Item ID chung.
            # ĐƯỢC PHÉP rỗng -> câu trả lời CHUNG (tên '__ASK_*', không gắn sản phẩm cụ thể).
            product = _clean(raw.get("product")) or (str(shopee_item_id).strip() if shopee_item_id else None)

            # Trả lời câu hỏi -> bắt buộc có loại câu hỏi hợp lệ (để dựng <sp>__ASK_*).
            if not is_intro(video_type, question_type):
                if not question_type:
                    raise ValueError("video_type='tra_loi' nhưng thiếu question_type (loại câu hỏi).")
                if resolve_intent(question_type) is None:
                    raise ValueError(f"question_type không hợp lệ: {question_type!r} "
                                     f"(dùng gia/mua/ship/... hoặc mã ASK_*).")

            rows.append({
                "row": excel_row,
                "product": product,            # tên base đã resolve (cột product hoặc item_id chung)
                "video_path": video,
                "video_type": video_type,
                "question_type": question_type,
                "text": text,
                "tts_provider": provider,
                "tts_voice": voice,
                "status": "ready",
            })
        except Exception as e:
            errors.append({"row": excel_row, "error": str(e)})

    return rows, errors


# ----------------------------------------------------------------- enqueue vào TTS queue

def submit_jobs(rows, shopee_item_id=None, progress=None, batch_id=None):
    """ENQUEUE mỗi dòng vào TTS queue bền vững (tts_jobs). KHÔNG gọi TTS tại chỗ —
    tts_worker.py sẽ synth (rate-limit + retry + adaptive throttle) rồi tạo render job.

    Trả (enqueued, warnings, batch_id). Voice được chuẩn hoá trước (voice guard): rỗng/cũ/
    ngoài whitelist -> thay bằng mặc định + cảnh báo (không drop dòng).
    """
    db.init_db()
    tts_db.init_db()
    TTS_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    if batch_id is None:
        batch_id = uuid.uuid4().hex[:12]

    enqueued, warnings = [], []
    for n, row in enumerate(rows, 1):
        provider = row["tts_provider"] or DEFAULT_PROVIDER
        voice, warn = normalize_voice(provider, row["tts_voice"])
        if warn:
            warnings.append({"row": row["row"], "warn": warn})
            _log_import_error(row["row"], f"voice guard: {warn}")
        tid = tts_db.enqueue(batch_id, row["row"], row["text"], provider, voice,
                             row["product"], row["video_path"], row["video_type"],
                             row["question_type"])
        enqueued.append({"tts_id": tid, "row": row["row"], "text": row["text"][:40]})
        if progress:
            progress(n, len(rows), f"Enqueue {n}/{len(rows)}")

    return enqueued, warnings, batch_id
