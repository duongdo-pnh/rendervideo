"""Read/write TTS provider config in .env (repo root) for the web UI config tab.

Lets the user fill API keys / URLs / default voices for all providers from the UI.
Saving updates .env, reloads it into the process, and resets the factory cache so the
new settings take effect immediately (no restart).
"""
import os
from pathlib import Path

ROOT = Path(__file__).parent
ENV_PATH = ROOT / ".env"

COMMON_FIELDS = [
    ("TTS_SPEED", "Tốc độ đọc chung (mặc định 1.2)", False),
]

# provider -> list of (env_key, label, is_secret). Order = display order in the UI.
PROVIDER_FIELDS = {
    "vbee": [
        ("VBEE_APP_ID", "App ID (UUID)", False),
        ("VBEE_TOKEN", "Access Token (JWT — bí mật)", True),
        ("VBEE_DEFAULT_VOICE", "Voice code mặc định", False),
        ("VBEE_WEBHOOK_URL", "Webhook URL (bắt buộc, có thể placeholder)", False),
        ("VBEE_SPEED", "Tốc độ riêng Vbee (trống = dùng TTS_SPEED)", False),
    ],
    "ausynclab": [
        # AusyncLab: API Key là phương thức xác thực DUY NHẤT (doc). Base URL cố định, không cần nhập.
        ("AUSYNCLAB_API_KEY", "API Key (ak_… — Master hoặc Sub key)", True),
        ("AUSYNCLAB_DEFAULT_VOICE", "Voice ID mặc định (lấy từ 'Kết nối & tải giọng')", False),
        ("AUSYNCLAB_MODEL", "Model AusyncLab (mặc định myna-1-turbo / Myna v1 Pro Fast)", False),
        ("AUSYNCLAB_SPEED", "Tốc độ riêng AusyncLab (trống = dùng TTS_SPEED, 0.75-1.25)", False),
        ("AUSYNCLAB_TIMEOUT", "Thời gian chờ AusyncLab hoàn tất audio, giây (mặc định 180)", False),
        ("AUSYNCLAB_REQUEST_TIMEOUT", "Timeout mỗi request AusyncLab, giây (mặc định 90)", False),
    ],
    "autovoice": [
        ("AUTOVOICE_API_KEY", "API Key Voice hệ thống (X-API-Key)", True),
        ("AUTOVOICE_DEFAULT_VOICE", "Mã giọng (voiceId)", False),
        ("AUTOVOICE_URL", "Endpoint TTS", False),
        ("AUTOVOICE_VOICES_URL", "Endpoint danh sách giọng (tuỳ chọn)", False),
        ("AUTOVOICE_SPEED", "Tốc độ riêng Voice hệ thống (trống = dùng TTS_SPEED)", False),
    ],
    "api": [
        ("TTS_API_KEY", "API Key", True),
        ("TTS_API_URL", "API URL", False),
        ("TTS_API_TYPE", "Loại (openai / elevenlabs)", False),
        ("TTS_API_DEFAULT_VOICE", "Giọng mặc định", False),
    ],
    "local": [
        ("LOCAL_TTS_ENGINE", "Engine (piper / coqui)", False),
        ("LOCAL_TTS_MODEL_PATH", "Model path", False),
        ("LOCAL_TTS_DEFAULT_VOICE", "Giọng mặc định", False),
    ],
}

PROVIDER_LABELS = {"vbee": "Vbee", "ausynclab": "Audiosynclab",
                   "autovoice": "Voice hệ thống",
                   "api": "API Online", "local": "Local (offline)"}

# Flat, ordered list of every env key the config tab manages.
ALL_KEYS = [k for (k, _, _) in COMMON_FIELDS] + [
    k for fields in PROVIDER_FIELDS.values() for (k, _, _) in fields
]


def current_values():
    """Hiện giá trị đang lưu trong .env cho mọi key (rỗng nếu chưa đặt).

    Read the file directly so the Gradio config tab does not show stale process
    environment values after saving and refreshing the browser.
    """
    try:
        from dotenv import dotenv_values
        raw = dotenv_values(str(ENV_PATH)) if ENV_PATH.exists() else {}
    except Exception:
        raw = {}
    return {k: "" if raw.get(k) is None else str(raw.get(k, "")) for k in ALL_KEYS}


def current_default_provider():
    try:
        from dotenv import dotenv_values
        raw = dotenv_values(str(ENV_PATH)) if ENV_PATH.exists() else {}
        return str(raw.get("DEFAULT_TTS_PROVIDER") or os.getenv("DEFAULT_TTS_PROVIDER", "vbee"))
    except Exception:
        return os.getenv("DEFAULT_TTS_PROVIDER", "vbee")


def save_config(values: dict, default_provider: str):
    """Ghi values + DEFAULT_TTS_PROVIDER vào .env, reload runtime, reset factory cache.

    Returns the refreshed provider-status list (available_providers()).
    """
    from dotenv import set_key, load_dotenv

    ENV_PATH.touch(exist_ok=True)
    # Persist (set_key giữ nguyên các dòng khác, cập nhật/thêm key cần thiết).
    set_key(str(ENV_PATH), "DEFAULT_TTS_PROVIDER", (default_provider or "vbee").strip())
    for k in ALL_KEYS:
        v = values.get(k)
        v = "" if v is None else str(v).strip()
        if k == "TTS_SPEED" and not v:
            v = "1.2"
        if k == "AUSYNCLAB_TIMEOUT" and not v:
            v = "180"
        if k == "AUSYNCLAB_REQUEST_TIMEOUT" and not v:
            v = "90"
        if k == "AUSYNCLAB_MODEL" and not v:
            v = "myna-1-turbo"
        set_key(str(ENV_PATH), k, v)
        os.environ[k] = v                       # áp dụng ngay cho tiến trình hiện tại
    os.environ["DEFAULT_TTS_PROVIDER"] = (default_provider or "vbee").strip()
    load_dotenv(str(ENV_PATH), override=True)

    # Reset factory: provider singletons phải dựng lại để đọc env mới.
    from latentsync.tts import factory
    factory._instances.clear()
    factory.DEFAULT_PROVIDER = os.environ["DEFAULT_TTS_PROVIDER"]

    return factory.available_providers()


def status_markdown(providers=None):
    """Bảng trạng thái provider (đã cấu hình chưa / giọng mặc định / provider mặc định)."""
    from latentsync.tts import factory
    providers = providers or factory.available_providers()
    lines = ["| Provider | Trạng thái | Giọng mặc định | Mặc định |",
             "|---|---|---|---|"]
    for p in providers:
        status = "✅ đã cấu hình" if p["enabled"] else "⚠ chưa cấu hình"
        star = "⭐" if p["is_default"] else ""
        lines.append(f"| {p['label']} | {status} | {p['default_voice'] or '—'} | {star} |")
    return "\n".join(lines)
