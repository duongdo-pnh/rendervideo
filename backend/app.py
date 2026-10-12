"""FastAPI backend for the render queue — replaces Gradio's built-in server.

Desktop-app model: this binds to 127.0.0.1 only (see run.py), no LAN/external
exposure needed. web_ui.py keeps running on its own port in parallel until the
new frontend (Phase 2) reaches feature parity — this is additive, not a cutover.
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import dashboard_reporter
import database as db

from . import akali, avatars, dashboard, drive, excel, jobs, livestream, tts_config_api, videos, workers, ws

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("rendervideo")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    workers.start_all()
    poll_task = asyncio.create_task(ws.poll_loop())
    try:
        yield
    finally:
        poll_task.cancel()


app = FastAPI(title="rendervideo API", lifespan=lifespan)
app.include_router(jobs.router)
app.include_router(videos.router)
app.include_router(excel.router)
app.include_router(excel.tts_router)
app.include_router(avatars.router)
app.include_router(livestream.router)
app.include_router(tts_config_api.router)
app.include_router(drive.router)
app.include_router(dashboard.router)
app.include_router(akali.router)
app.include_router(ws.router)


# Máy chưa có INGEST_TOKEN = chưa đăng ký với dashboard fleet -> khoá toàn bộ API.
# Chặn ở đây chứ không chỉ ẩn nút trên giao diện: giao diện chỉ là một client,
# ẩn nút không ngăn được ai gọi thẳng API.
#
# Chừa đúng 3 đường để bản thân màn hình kích hoạt còn chạy được: đọc trạng thái,
# lưu token, và health check. Không chặn đường ngoài /api (trang tĩnh, /ws) —
# chặn trang tĩnh thì không load nổi chính cái màn hình nhập token.
_NODE_KEY_OPEN_PATHS = frozenset({
    "/api/health",
    "/api/dashboard/status",
    "/api/dashboard/config",
})


@app.middleware("http")
async def require_node_key(request: Request, call_next):
    path = request.url.path
    if (path.startswith("/api/")
            and path not in _NODE_KEY_OPEN_PATHS
            and not dashboard_reporter.dashboard_enabled()):
        logger.warning("403 %s %s: máy chưa nhập Ingest Token", request.method, path)
        return JSONResponse(
            status_code=403,
            content={"detail": "Máy chưa kích hoạt — nhập Ingest Token (Node key) để dùng."},
        )
    return await call_next(request)


# Every response an endpoint sends back today already goes through one of
# these three shapes (validation, explicit HTTPException, or an uncaught bug).
# Centralizing them here means every request path is logged server-side and
# every client-facing `detail` is guaranteed to be a plain, Vietnamese string
# — endpoints no longer need their own try/except just to avoid leaking a raw
# Pydantic error list or a bare Python exception message to the UI.
@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    logger.warning("422 %s %s: %s", request.method, request.url.path, exc.errors())
    fields = ", ".join(str(e["loc"][-1]) for e in exc.errors() if e.get("loc"))
    detail = f"Dữ liệu gửi lên không hợp lệ ở trường: {fields}" if fields else "Dữ liệu gửi lên không hợp lệ."
    return JSONResponse(status_code=422, content={"detail": detail})


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    log = logger.error if exc.status_code >= 500 else logger.warning
    log("%s %s %s: %s", exc.status_code, request.method, request.url.path, exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("500 %s %s (unhandled)", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Máy chủ đang gặp sự cố, vui lòng thử lại sau."})


@app.get("/api/health")
def health():
    return {"ok": True}


# Serve the built React frontend (frontend/dist, from `npm run build`) on the
# SAME port as the API — the Tauri desktop shell just points its window at
# this one URL. Dev keeps using the Vite server (5173) proxying to here, so
# this only activates once a production build actually exists; it never
# interferes with `npm run dev`.
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"

if FRONTEND_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="frontend-assets")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str):
        """Client-side routing (React Router) — serve the real file if the
        build emitted one (favicon.svg, ...), else fall back to index.html so
        deep links like /videos resolve on the client instead of 404ing."""
        candidate = FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")
