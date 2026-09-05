"""Entrypoint: python -m backend.run

Binds 127.0.0.1 only (desktop-app model, see app.py). Port matches BACKEND_PORT
below — keep this in sync with any frontend/dev-proxy config added in Phase 2.
"""
import os

import uvicorn

BACKEND_HOST = os.environ.get("BACKEND_HOST", "127.0.0.1")
BACKEND_PORT = int(os.environ.get("BACKEND_PORT", "8000"))

if __name__ == "__main__":
    uvicorn.run("backend.app:app", host=BACKEND_HOST, port=BACKEND_PORT, reload=False)
