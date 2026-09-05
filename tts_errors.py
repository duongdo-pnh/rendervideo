"""Phân loại lỗi TTS: nên retry (tạm thời) hay hỏng vĩnh viễn — và rút status code + Retry-After.

Dùng bởi tts_worker để quyết định retry/backoff. Ưu tiên đọc thuộc tính .status_code /
.retry_after mà provider gắn vào exception; nếu không có thì dò trong chuỗi lỗi.
"""
import re

import requests

# HTTP code NÊN retry (lỗi tạm thời) vs HỎNG HẲN (payload/quyền/voice sai -> retry vô ích).
RETRYABLE_CODES = {408, 425, 429, 500, 502, 503, 504}
PERMANENT_CODES = {400, 401, 403, 404, 422}

_CODE_RE = re.compile(r"\b([45]\d\d)\b")
_RETRY_AFTER_RE = re.compile(r"retry[-_ ]?after[\"':=\s]+(\d+(?:\.\d+)?)", re.I)


def parse_status(exc):
    """HTTP status code của lỗi (ưu tiên .status_code, else token 3 chữ số 4xx/5xx đầu tiên)."""
    code = getattr(exc, "status_code", None)
    if code:
        try:
            return int(code)
        except (TypeError, ValueError):
            pass
    m = _CODE_RE.search(str(exc))
    return int(m.group(1)) if m else None


def parse_retry_after(exc):
    """Giây cần chờ theo Retry-After (None nếu không có)."""
    ra = getattr(exc, "retry_after", None)
    if ra is None:
        m = _RETRY_AFTER_RE.search(str(exc))
        ra = m.group(1) if m else None
    if ra is None:
        return None
    try:
        v = float(ra)
        return v if v >= 0 else None
    except (TypeError, ValueError):
        return None


def is_retryable(exc):
    """True nếu lỗi tạm thời (nên retry). Không có HTTP code -> coi là network/timeout -> retry."""
    explicit = getattr(exc, "retryable", None)
    if explicit is not None:
        return bool(explicit)
    code = parse_status(exc)
    if code is None:
        return True                  # network error / timeout
    if code in PERMANENT_CODES:
        return False
    if code in RETRYABLE_CODES:
        return True
    return code >= 500               # 5xx lạ -> retry; 4xx lạ -> permanent


_PROVIDER_LABEL = {"vbee": "Vbee", "ausynclab": "AusyncLab", "autovoice": "AutoVoice"}

# Người dùng cuối (nhân viên vận hành, không phải dev) đọc last_error qua
# /api/tts/status -> ExcelPage — "API key", "rate limit", hay một mã HTTP trần
# trụi (vd "lỗi HTTP 409") đều vô nghĩa với họ. describe() vì vậy CHỈ trả về
# 1 trong số câu tiếng Việt cố định dưới đây (không lắp chuỗi lỗi gốc của
# provider vào câu chính) + tối đa một mã số ngắn để tiện báo hỗ trợ kỹ
# thuật. Bản đầy đủ (JSON/HTML/traceback provider trả) không hiển thị ở đây —
# luôn còn nguyên trong log của tts_worker (_log) cho ai cần debug sâu.
_CONFIG_MARKERS = (".env", "Cấu hình TTS", "Cau hinh TTS")
_INPUT_MARKERS = ("rong.", "rỗng", "Chua nhap", "Chưa nhập", "phai la", "phải là")


def describe(exc, provider=None):
    """Thông báo lỗi TTS ngắn gọn, tiếng Việt phổ thông, cho người dùng cuối.

    - Có .status_code (provider trả lỗi HTTP thật) -> phân loại theo nhóm mã
      (auth / not-found / input sai / quá tải / server lỗi) thành câu sẵn.
    - Không có status_code nhưng là lỗi mạng/socket thật (requests.exceptions)
      -> câu "không kết nối được".
    - Không có status_code và KHÔNG phải lỗi mạng (thiếu cấu hình, thiếu văn
      bản/giọng, response provider thiếu field, poll timeout...) -> dò vài từ
      khoá cố định trong message provider tự viết để tách "cần sửa cấu hình"
      / "cần sửa nội dung dòng này" khỏi nhóm còn lại (mọi trục trặc phía
      provider) chứ không lộ message gốc.
    """
    label = _PROVIDER_LABEL.get(provider, provider or "TTS")

    if getattr(exc, "reason", None) == "text_too_long":
        return f"{label}: {exc}"

    code = parse_status(exc)

    if code is None:
        if isinstance(exc, requests.exceptions.RequestException):
            return f"{label}: không kết nối được tới máy chủ giọng đọc. Hệ thống sẽ tự thử lại."
        text = str(exc)
        if any(m in text for m in _CONFIG_MARKERS):
            return f"{label}: chưa cấu hình đủ thông tin kết nối. Vào tab Cấu hình TTS để kiểm tra lại."
        if any(m in text for m in _INPUT_MARKERS):
            return f"{label}: nội dung hoặc giọng đọc của dòng này chưa hợp lệ. Kiểm tra lại dòng trong Excel."
        return f"{label} đang gặp sự cố, hệ thống sẽ tự thử lại."

    if code in (401, 403):
        base = f"{label}: sai thông tin kết nối. Vào tab Cấu hình TTS để kiểm tra lại"
    elif code == 404:
        base = f"{label}: không tìm thấy giọng đọc đã chọn — hãy chọn giọng khác"
    elif code in (400, 422):
        base = f"{label}: nội dung hoặc giọng đọc của dòng này chưa hợp lệ"
    elif code == 429:
        base = f"{label} đang quá tải, hệ thống sẽ tự thử lại sau"
    elif code >= 500:
        base = f"{label} đang gặp sự cố tạm thời, hệ thống sẽ tự thử lại"
    else:
        base = f"{label} báo lỗi, hệ thống sẽ tự thử lại"
    return f"{base} (mã lỗi {code})."
