"""Phân loại lỗi TTS: nên retry (tạm thời) hay hỏng vĩnh viễn — và rút status code + Retry-After.

Dùng bởi tts_worker để quyết định retry/backoff. Ưu tiên đọc thuộc tính .status_code /
.retry_after mà provider gắn vào exception; nếu không có thì dò trong chuỗi lỗi.
"""
import re

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
    code = parse_status(exc)
    if code is None:
        return True                  # network error / timeout
    if code in PERMANENT_CODES:
        return False
    if code in RETRYABLE_CODES:
        return True
    return code >= 500               # 5xx lạ -> retry; 4xx lạ -> permanent
