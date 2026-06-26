"""Voice guard: chuẩn hoá voice_id TRƯỚC khi gọi TTS, để Excel ghi cứng voice cũ/sai
không phá job. Quy tắc (theo tts_voice_config.json):

  - rỗng           -> default_voice_id
  - trong map cũ   -> voice mới (deprecated_voice_map)
  - ngoài whitelist (nếu allowed_voice_ids khác rỗng) -> thay bằng default + cảnh báo

KHÔNG drop dòng: voice lạ vẫn được thay bằng default rồi chạy tiếp (mục tiêu: không mất dòng).
Trả (voice_id_chuẩn, warning|None).
"""
import json
from pathlib import Path

_CONFIG_PATH = Path(__file__).parent / "tts_voice_config.json"
_cache = None


def _load():
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            _cache = {}
    return _cache


def normalize_voice(provider, voice):
    """-> (voice_id, warning). Không raise; provider không có config thì trả nguyên voice."""
    cfg = _load().get((provider or "").lower().strip())
    if not cfg:
        return (str(voice).strip() if voice else None), None

    default = cfg.get("default_voice_id") or None
    dep = cfg.get("deprecated_voice_map") or {}
    allow = cfg.get("allowed_voice_ids") or []

    v = str(voice).strip() if voice else ""
    if not v:
        return default, None
    if v in dep:
        return dep[v], f"voice '{v}' đã cũ -> dùng '{dep[v]}'"
    if allow and v not in allow:
        return default, f"voice '{v}' ngoài whitelist -> dùng mặc định '{default}'"
    return v, None
