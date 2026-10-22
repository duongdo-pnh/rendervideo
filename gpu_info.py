"""Runtime GPU identification for the backend — separate from install/detect_gpu.py's
richer install-time detection, which lives under install/ and isn't meant to be
imported from the running app.
"""
from functools import lru_cache
import logging
import os
import subprocess

logger = logging.getLogger("rendervideo")

# LatentSync has a known-bad interaction with the plain RTX 3080 (crashes that don't
# happen on other sm_86 cards like the RTX 3090 — see install/gpu_profiles.json, which
# pins them all to the same torch build despite this). Block the option in the UI rather
# than let users hit it. The RTX 3080 Ti is allowed: LatentSync 1.6 is verified on it with
# VAE slicing + the LATENTSYNC_VRAM_FRACTION cap (scripts/inference.py) inside 12 GB.
LATENTSYNC_BLOCKED_SUBSTRING = "RTX 3080"
LATENTSYNC_ALLOWED_SUBSTRING = "RTX 3080 Ti"

# Cards below this get the low-VRAM path: LatentSync runs with VAE slicing + a PyTorch VRAM
# cap, and pauses the MuseTalk server while it renders. LatentSync 1.6 at 512px OOMs on 12 GB
# (RTX 3080 Ti) without it; 24 GB cards (RTX 3090) keep the faster path unchanged.
LOW_VRAM_THRESHOLD_GIB = 16


@lru_cache(maxsize=1)
def gpu_total_gib() -> float:
    """First GPU's total VRAM in GiB from nvidia-smi (no CUDA context needed), 0 if unknown."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10, check=True,
        )
        return float(out.stdout.splitlines()[0].strip()) / 1024
    except Exception:
        return 0.0


def is_low_vram_gpu() -> bool:
    total = gpu_total_gib()
    return 0 < total < LOW_VRAM_THRESHOLD_GIB


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
    """True on a plain RTX 3080 (not the Ti). Set BYSCOM_ALLOW_LATENTSYNC=1 to lift the
    block for testing on that card."""
    if os.environ.get("BYSCOM_ALLOW_LATENTSYNC") == "1":
        return False
    name = current_gpu_name()
    blocked = LATENTSYNC_BLOCKED_SUBSTRING in name and LATENTSYNC_ALLOWED_SUBSTRING not in name
    if blocked:
        logger.warning("LatentSync disabled on %r (known crashes on RTX 3080); "
                       "set BYSCOM_ALLOW_LATENTSYNC=1 to override", name)
    return blocked
