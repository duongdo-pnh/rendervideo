"""Read/write TTS provider config in .env (repo root) for the web UI config tab.

Lets the user fill API keys / URLs / default voices for all 4 providers from the UI.
Saving updates .env, reloads it into the process, and resets the factory cache so the
new settings take effect immediately (no restart).
"""
import os
from pathlib import Path

ROOT = Path(__file__).parent
ENV_PATH = ROOT / ".env"

# provider -> list of (env_key, label, is_secret). Order = display order in the UI.
PROVIDER_FIELDS = {
    "vbee": [
        ("VBEE_APP_ID", "App ID (UUID)", False),
        ("VBEE_TOKEN", "Access Token (JWT — bí mật)", True),
        ("VBEE_DEFAULT_VOICE", "Voice code mặc định", False),
        ("VBEE_WEBHOOK_URL", "Webhook URL (bắt buộc, có thể placeholder)", False),
        ("VBEE_SPEED", "Tốc độ (vd 1.0)", False),
    ],
    "ausynclab": [
        # AusyncLab: API Key là phương thức xác thực DUY NHẤT (doc). Base URL cố định, không cần nhập.
        ("AUSYNCLAB_API_KEY", "API Key (ak_… — Master hoặc Sub key)", True),
        ("AUSYNCLAB_DEFAULT_VOICE", "Voice ID mặc định (lấy từ 'Kết nối & tải giọng')", False),
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
                   "api": "API Online", "local": "Local (offline)"}

# Flat, ordered list of every env key the config tab manages.
ALL_KEYS = [k for fields in PROVIDER_FIELDS.values() for (k, _, _) in fields]


def current_values():
    """Hiện giá trị env đang dùng cho mọi key (rỗng nếu chưa đặt)."""
    return {k: os.getenv(k, "") for k in ALL_KEYS}


def current_default_provider():
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
    """Bảng trạng thái 4 provider (đã cấu hình chưa / giọng mặc định / provider mặc định)."""
    from latentsync.tts import factory
    providers = providers or factory.available_providers()
    lines = ["| Provider | Trạng thái | Giọng mặc định | Mặc định |",
             "|---|---|---|---|"]
    for p in providers:
        status = "✅ đã cấu hình" if p["enabled"] else "⚠ chưa cấu hình"
        star = "⭐" if p["is_default"] else ""
        lines.append(f"| {p['label']} | {status} | {p['default_voice'] or '—'} | {star} |")
    return "\n".join(lines)
