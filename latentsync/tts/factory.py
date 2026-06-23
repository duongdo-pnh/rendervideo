"""TTS factory — pick & cache a provider by name, driven by env config.

Loads .env (repo root) on import so the providers' os.getenv() calls see it.
"""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    # Repo root = two levels up from this file (latentsync/tts/factory.py).
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except Exception:
    pass  # dotenv optional; env may already be set by the shell.

from .base import TTSProvider

DEFAULT_PROVIDER = os.getenv("DEFAULT_TTS_PROVIDER", "vbee")

# name -> (label, default-voice env). "enabled" computed by _is_enabled (Vbee needs App ID + Token).
_PROVIDER_META = {
    "vbee":      ("Vbee",          "VBEE_DEFAULT_VOICE"),
    "ausynclab": ("Audiosynclab",  "AUSYNCLAB_DEFAULT_VOICE"),
    "api":       ("API Online",    "TTS_API_DEFAULT_VOICE"),
    "local":     ("Local",         "LOCAL_TTS_DEFAULT_VOICE"),  # no key needed
}


def _is_enabled(name):
    if name == "vbee":
        return bool(os.getenv("VBEE_APP_ID") and os.getenv("VBEE_TOKEN"))
    if name == "ausynclab":
        return bool(os.getenv("AUSYNCLAB_API_KEY"))
    if name == "api":
        return bool(os.getenv("TTS_API_KEY"))
    if name == "local":
        return True
    return False

_instances = {}


def get_provider(name: str = None) -> TTSProvider:
    name = (name or DEFAULT_PROVIDER).lower().strip()
    if name not in _instances:
        if name == "vbee":
            from .vbee import VbeeTTS
            _instances[name] = VbeeTTS()
        elif name == "ausynclab":
            from .ausynclab import AusynclabTTS
            _instances[name] = AusynclabTTS()
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
    return get_provider(provider).synthesize(text, output_path, voice)


def available_providers():
    """List provider config status (for the UI / a /providers view).

    enabled = key present (or no key required, e.g. local)."""
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
    try:
        return get_provider(provider).voices()
    except Exception:
        meta = _PROVIDER_META.get((provider or "").lower().strip())
        dv = os.getenv(meta[1]) if (meta and meta[1]) else None
        return [dv] if dv else []
