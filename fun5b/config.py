"""Paths, output spec and process environment of the Fun-5B engine (replaces temo's wan_animate/config.py).

Everything heavy lives outside git, next to the other engines (see docs/FUN5B_INTEGRATION_PLAN.md §2):
  checkpoints/fun5b/      models (wan22-fun-5b/, firered-image-edit/, qwen-image-edit/), ~54GB
  engines/VideoX-Fun/     upstream checkout pinned in install/fun5b-source.json
  engines/Fun5B/.venv     this package's interpreter (Python 3.11, torch 2.8.0+cu128)
  engines/Fun5B/.venv-edit  FireRed's interpreter (fun5b/edit_worker.py)
  fun5b_cache/            pose detections, clean plates, Triton/Inductor compile results
Each can be moved with an env var (FUN5B_CKPT_DIR, FUN5B_VIDEOX_DIR, FUN5B_EDIT_PYTHON, FUN5B_CACHE_DIR).
"""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # the rendervideo checkout / install dir

CKPT_DIR = os.environ.get("FUN5B_CKPT_DIR") or os.path.join(ROOT, "checkpoints/fun5b")
VIDEOX_DIR = os.environ.get("FUN5B_VIDEOX_DIR") or os.path.join(ROOT, "engines/VideoX-Fun")
ENGINE_DIR = os.path.join(ROOT, "engines/Fun5B")
PYTHON = os.path.join(ENGINE_DIR, ".venv/bin/python")
EDIT_PYTHON = os.environ.get("FUN5B_EDIT_PYTHON") or os.path.join(ENGINE_DIR, ".venv-edit/bin/python")
CACHE_DIR = os.environ.get("FUN5B_CACHE_DIR") or os.path.join(ROOT, "fun5b_cache")
POSE_CACHE_DIR = os.path.join(CACHE_DIR, "poses")
PLATE_CACHE_DIR = os.path.join(CACHE_DIR, "plates")

# Output spec: always 30fps (the size is fixed in fun5b.run: 720×1280).
FPS = 30
DEFAULT_SEED = 42

# H.264 output as BT.709, tagged: untagged files are encoded with ffmpeg's BT.601 default but players assume BT.709 for
# HD, which shifts and dulls the colours.
VIDEO_COLOR_PARAMS = ["-vf", "scale=out_color_matrix=bt709:out_range=tv", "-colorspace", "bt709",
                      "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]

# Triton compiles its launchers with the system C compiler (gcc, from install/setup.sh's apt step).
# Keep Triton/Inductor compile results across restarts (default is /tmp).
os.environ.setdefault("TORCHINDUCTOR_CACHE_DIR", os.path.join(CACHE_DIR, "compile/inductor"))
os.environ.setdefault("TRITON_CACHE_DIR", os.path.join(CACHE_DIR, "compile/triton"))
