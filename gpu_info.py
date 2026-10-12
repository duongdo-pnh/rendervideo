"""Runtime GPU identification for the backend — separate from install/detect_gpu.py's
richer install-time detection, which lives under install/ and isn't meant to be
imported from the running app.
"""
from functools import lru_cache
import subprocess

# LatentSync has a known-bad interaction with these specific cards (crashes that
# don't happen on other sm_86 cards like the RTX 3090 — see install/gpu_profiles.json,
# which pins all three to the same torch build despite this). Block the option in the
# UI rather than let users hit it.
LATENTSYNC_BLOCKED_SUBSTRING = "RTX 3080"


@lru_cache(maxsize=1)
def current_gpu_name() -> str:
    """First GPU's name reported by nvidia-smi, or '' if unavailable. Cached: the
    installed GPU can't change while this process is running."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10, check=True,
        )
        return out.stdout.splitlines()[0].strip()
    except Exception:
        return ""


@lru_cache(maxsize=1)
def latentsync_blocked() -> bool:
    """True on RTX 3080 / RTX 3080 Ti (substring match covers both)."""
    return LATENTSYNC_BLOCKED_SUBSTRING in current_gpu_name()
