"""Clean-plate cache key, shared by fun5b/plate.py (engine venv) and the app backend (main venv):
the backend uses it to tell the UI an image already has a saved clean background. Kept free of
any import beyond the standard library so the main venv can load it without fun5b's heavy deps."""

import hashlib

LOCAL_PROMPT = ("Remove the person completely and show the background that was behind them. Do not add any new objects. "
                "Keep everything else in the image exactly the same: same framing, same colours, same lighting.")
LOCAL_STEPS, LOCAL_CFG, LOCAL_SIZE, SEED = 40, 4.0, "768x1376", 0
RENDER_W, RENDER_H = 640, 1152  # fun5b.run's render size: the key is over the reference fitted to it


def cache_key(fitted_bytes):
    """Cache id of a reference fitted to RENDER_W×RENDER_H (uint8 RGB bytes, as ImageOps.fit + LANCZOS)."""
    spec = f"firered-1.1-q5|{LOCAL_PROMPT}|{LOCAL_STEPS}|{LOCAL_CFG}|{LOCAL_SIZE}|{SEED}"
    return hashlib.sha256(fitted_bytes + spec.encode()).hexdigest()[:32]
