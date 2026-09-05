"""Shared job-naming rules for the render queue.

The output filename encodes product + intent so the live system can auto-match
video <-> product on import: "<product>__<INTENT>" (or just "<product>" for an
intro video). Both web_ui.py (Gradio) and the FastAPI backend need the exact
same naming so a job submitted through either UI matches the same way.
"""
import re

import excel_import

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


def slug_key(value):
    key = excel_import._ascii(value).replace(" ", "_").replace("-", "_")
    key = re.sub(r"[^a-z0-9_]+", "_", key)
    key = re.sub(r"_+", "_", key).strip("_")
    return key or None


def manual_intent(intent, other_key=None):
    if intent == "ASK_OTHER":
        key = slug_key(other_key)
        return f"ASK_OTHER_{key}" if key else "ASK_OTHER"
    return intent


def build_name(product, kind, intent, other_key=None):
    """Dựng tên chuẩn từ ô nhập: '<sản phẩm>__<INTENT>' (Trả lời) hoặc '<sản phẩm>' (Giới thiệu)."""
    product = (product or "").strip()
    if kind == KIND_ANSWER and intent:
        intent = manual_intent(intent, other_key)
        return f"{product}__{intent}" if product else f"__{intent}"
    return product or "video"
