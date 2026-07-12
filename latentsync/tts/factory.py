"""TTS factory — pick & cache a provider by name, driven by env config.

Loads .env (repo root) on import so the providers' os.getenv() calls see it.
"""
import os
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
_env_mtime = None

try:
    from dotenv import load_dotenv
    # Repo root = two levels up from this file (latentsync/tts/factory.py).
    load_dotenv(ENV_PATH, override=True)
except Exception:
    pass  # dotenv optional; env may already be set by the shell.

from .base import TTSProvider

DEFAULT_PROVIDER = os.getenv("DEFAULT_TTS_PROVIDER", "vbee")

# name -> (label, default-voice env). "enabled" computed by _is_enabled.
_PROVIDER_META = {
    "vbee":      ("Vbee",          "VBEE_DEFAULT_VOICE"),
    "ausynclab": ("Audiosynclab",  "AUSYNCLAB_DEFAULT_VOICE"),
    "autovoice": ("Voice hệ thống", "AUTOVOICE_DEFAULT_VOICE"),
    "api":       ("API Online",    "TTS_API_DEFAULT_VOICE"),
    "local":     ("Local",         "LOCAL_TTS_DEFAULT_VOICE"),  # no key needed
}


def _is_enabled(name):
    if name == "vbee":
        return bool(os.getenv("VBEE_APP_ID") and os.getenv("VBEE_TOKEN"))
    if name == "ausynclab":
        return bool(os.getenv("AUSYNCLAB_API_KEY"))
    if name == "autovoice":
        return bool(os.getenv("AUTOVOICE_API_KEY") and os.getenv("AUTOVOICE_DEFAULT_VOICE"))
    if name == "api":
        return bool(os.getenv("TTS_API_KEY"))
    if name == "local":
        return True
    return False

_instances = {}


def reload_config(force=False):
    """Reload .env when it changes and rebuild cached providers.

    TTS workers are long-lived. Without this, changing TTS_SPEED or provider settings
    from the UI only affects the web process, while the worker keeps old provider
    singletons. This check is cheap and runs before provider access.
    """
    global DEFAULT_PROVIDER, _env_mtime
    try:
        mtime = ENV_PATH.stat().st_mtime
    except OSError:
        mtime = None

    if not force and mtime == _env_mtime:
        return False

    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_PATH, override=True)
    except Exception:
        pass

    DEFAULT_PROVIDER = os.getenv("DEFAULT_TTS_PROVIDER", "vbee")
    _instances.clear()
    _env_mtime = mtime
    return True


def get_provider(name: str = None) -> TTSProvider:
    reload_config()
    name = (name or DEFAULT_PROVIDER).lower().strip()
    if name not in _instances:
        if name == "vbee":
            from .vbee import VbeeTTS
            _instances[name] = VbeeTTS()
        elif name == "ausynclab":
            from .ausynclab import AusynclabTTS
            _instances[name] = AusynclabTTS()
        elif name == "autovoice":
            from .autovoice import AutoVoiceTTS
            _instances[name] = AutoVoiceTTS()
        elif name == "api":
            from .api_online import ApiOnlineTTS
            _instances[name] = ApiOnlineTTS()
        elif name == "local":
            from .local_tts import LocalTTS
            _instances[name] = LocalTTS()
        else:
            raise ValueError(f"Unknown TTS provider: {name}")
    return _instances[name]


def synthesize(text, output_path, provider=None, voice=None):
    reload_config()
    return get_provider(provider).synthesize(text, output_path, voice)


def available_providers():
    """List provider config status (for the UI / a /providers view).

    enabled = key present (or no key required, e.g. local)."""
    reload_config()
    out = []
    for name, (label, voice_env) in _PROVIDER_META.items():
        out.append({
            "name": name,
            "label": label,
            "enabled": _is_enabled(name),
            "default_voice": os.getenv(voice_env) if voice_env else None,
            "is_default": name == (DEFAULT_PROVIDER or "").lower().strip(),
        })
    return out


def list_voices(provider: str):
    """Known voice codes for a provider (best-effort; many APIs need a live call to enumerate)."""
    reload_config()
    try:
        return get_provider(provider).voices()
    except Exception:
        meta = _PROVIDER_META.get((provider or "").lower().strip())
        dv = os.getenv(meta[1]) if (meta and meta[1]) else None
        return [dv] if dv else []
