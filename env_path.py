"""Where .env secrets/config actually live — shared by google_drive_upload.py,
tts_config.py and latentsync/tts/factory.py (all hit the same bug independently).

The packaged desktop app extracts a fresh source tree into a versioned install
dir on every rebuild/reinstall (desktop/scripts/bundle-source.sh only bundles
git-tracked files, and .env is gitignored — it's never included), and that
whole dir gets wiped+recreated on updates. A .env sitting next to a script
would vanish on the next rebuild, losing whatever was saved through the UI.
XDG_CONFIG_HOME (~/.config by default) isn't touched by app reinstalls.
"""
import os
from pathlib import Path


def resolve_env_path(local_path) -> Path:
    """local_path: the dev-checkout .env location (e.g. repo root / ".env").

    Returns local_path unchanged if it exists there (dev checkout, unchanged
    behavior). Otherwise falls back to a persistent per-user path that
    survives app reinstalls — used for both reads and writes when running as
    the packaged app.
    """
    local_path = Path(local_path)
    if local_path.exists():
        return local_path
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config_home) / "byscom-render" / ".env"
