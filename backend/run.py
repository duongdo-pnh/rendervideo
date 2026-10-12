"""Entrypoint: python -m backend.run

Binds 127.0.0.1 only (desktop-app model, see app.py). Port matches BACKEND_PORT
below — keep this in sync with any frontend/dev-proxy config added in Phase 2.
"""
import os
from pathlib import Path

import uvicorn

BACKEND_HOST = os.environ.get("BACKEND_HOST", "127.0.0.1")
BACKEND_PORT = int(os.environ.get("BACKEND_PORT", "8000"))

# Not an flock (unlike queue_worker.py/tts_worker.py's own lock files) — just a
# startup marker so the Tauri shell can tell whether the backend already
# listening on BACKEND_PORT predates the last ensure_source() refresh and, if
# so, kill it before reusing it (see desktop/src-tauri/src/lib.rs's
# kill_stale_backend()). Without this, reinstalling a newer .deb over an
# install whose backend never got killed (window close doesn't stop it — see
# lib.rs's module docstring) reconnects to the SAME old process, which keeps
# running whatever code was in memory when it started, forever.
PID_FILE = Path(__file__).resolve().parent.parent / ".backend_run.pid"

if __name__ == "__main__":
    PID_FILE.write_text(str(os.getpid()))
    try:
        uvicorn.run("backend.app:app", host=BACKEND_HOST, port=BACKEND_PORT, reload=False)
    finally:
        if PID_FILE.read_text().strip() == str(os.getpid()):
            PID_FILE.unlink(missing_ok=True)
