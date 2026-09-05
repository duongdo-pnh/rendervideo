"""MuseTalk livestream control endpoints — proxies musetalk_stream_api.py's own
HTTP API, same as stream_ui.py's Gradio callbacks do.

Reuses stream_ui.py's helpers directly (_ensure_backend, _request,
_normalize_facebook_avatar, _compose_push_url, _sample_audio_choices) rather
than re-implementing the same proxy-of-a-proxy logic — those functions have no
Gradio UI dependency of their own, they only signal errors via gr.Error (a
ValueError subclass), which this router catches and converts to HTTPException.

Caveat: stream_ui.py has a couple of module-level globals (_CURRENT_PREVIEW_SESSION,
_CURRENT_AVATAR_VIDEO) used only as a fallback preview frame before the MuseTalk
backend itself is ready. Since this FastAPI process and web_ui.py's Gradio process
each import their own separate copy of that module, those two globals are NOT
shared across the old and new UI if both are used at once — a session started from
one won't populate the other's fallback frame. Not an issue once web_ui.py is
retired (Phase 3); until then, prefer starting/watching a given stream from one UI.
"""
from pathlib import Path

import gradio as gr
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

import job_files
import stream_ui

from .models import LivestreamEnqueueResponse, LivestreamEnqueueSampleRequest, SampleAudioChoice

router = APIRouter(prefix="/api/livestream", tags=["livestream"])


def _call(fn, *args, **kwargs):
    """Run a stream_ui helper, translating its gr.Error convention to HTTPException."""
    try:
        return fn(*args, **kwargs)
    except gr.Error as e:
        raise HTTPException(409, str(e)) from None


@router.get("/sample-audio", response_model=list[SampleAudioChoice])
def sample_audio():
    return [SampleAudioChoice(label=label, path=path) for label, path in stream_ui._sample_audio_choices()]


@router.get("/server/status", response_model=dict)
def server_status():
    return {"running": stream_ui._backend_ready()}


@router.post("/server/start", response_model=dict)
def server_start():
    """Manual warm-up: musetalk_stream_api.py otherwise only auto-starts lazily
    the first time a stream session is started, which makes that first
    'Bắt đầu stream' click wait for it (up to 3 minutes) — this lets it be
    started ahead of time instead."""
    _call(stream_ui._ensure_backend)
    return {"running": True}


@router.post("/server/stop", response_model=dict)
def server_stop():
    """Shuts down the whole server process, not a specific session — any
    stream currently live on it goes down too. Frees the GPU memory it pins
    when nobody needs it running."""
    stream_ui._stop_backend()
    return {"running": False}


@router.post("/{session_id}/start", response_model=dict)
async def start_stream(
    session_id: str,
    avatar_video: UploadFile | None = File(None),
    server_url: str = Form(""),
    stream_key: str = Form(""),
    render_ahead_seconds: float = Form(2.0),
    batch_size: int = Form(6),
    obs_output_fps: int = Form(30),
    audio_delay_ms: int = Form(0),
    avatar_max_seconds: float = Form(0),
):
    """Starts avatar prep (may take up to ~40 min for a fresh clip) and returns
    immediately — musetalk_stream_api.py preps in its own background thread.
    Poll GET /{session_id}/status same as stream_ui.py's start_stream() generator
    does internally."""
    session_id = session_id.strip()
    if not session_id:
        raise HTTPException(422, "Cần nhập Session ID.")

    avatar_path = None
    if avatar_video is not None and avatar_video.filename:
        video_dir = job_files.UPLOADS_DIR / "videos"
        video_dir.mkdir(parents=True, exist_ok=True)
        avatar_path = job_files.write_default_video(avatar_video.filename, await avatar_video.read())

    if session_id == "facebook-live":
        avatar_path = _call(stream_ui._normalize_facebook_avatar, avatar_path)

    if not avatar_path or not Path(avatar_path).is_file():
        raise HTTPException(422, "Cần tải lên avatar video hợp lệ.")

    stream_ui._CURRENT_PREVIEW_SESSION = session_id
    stream_ui._CURRENT_AVATAR_VIDEO = str(Path(avatar_path).resolve())

    use_obs_udp = session_id == "facebook-live" and not server_url.strip() and not stream_key.strip()
    use_relive = not use_obs_udp and not server_url.strip() and not stream_key.strip()
    if use_obs_udp:
        push_url = "udp://127.0.0.1:5000?pkt_size=1316"
    elif use_relive:
        push_url = ""
    else:
        push_url = _call(stream_ui._compose_push_url, server_url, stream_key)

    _call(stream_ui._ensure_backend)
    return _call(stream_ui._request, "POST", "/api/streams", {
        "session_id": session_id,
        "avatar_id": session_id,
        "avatar_video": str(Path(avatar_path).resolve()),
        "push_url": push_url,
        "output_mode": "udp" if use_obs_udp else ("relive" if use_relive else "rtmp"),
        "fps": 30,
        "output_fps": int(obs_output_fps) if obs_output_fps else 30,
        "warmup_frames": (
            30 if session_id == "facebook-live"
            else min(250, max(0, int(round(render_ahead_seconds * 30))))
        ),
        "batch_size": int(batch_size),
        "audio_delay_ms": int(audio_delay_ms),
        "avatar_max_seconds": float(avatar_max_seconds) if avatar_max_seconds else 0,
    }, timeout=30)


@router.get("/{session_id}/status", response_model=dict)
def stream_status(session_id: str):
    return _call(stream_ui._request, "GET", f"/api/streams/{session_id.strip()}")


@router.get("/{session_id}/preview.jpg")
def stream_preview(session_id: str):
    """Live frame from musetalk_stream_api.py if the stream is up, else a still
    frame from the avatar video itself (mirrors stream_ui.preview_stream())."""
    import urllib.error
    import urllib.request

    if stream_ui._backend_ready():
        try:
            with urllib.request.urlopen(
                f"{stream_ui.BACKEND_URL}/api/streams/{session_id.strip()}/preview.jpg", timeout=2
            ) as resp:
                return Response(content=resp.read(), media_type="image/jpeg")
        except (OSError, urllib.error.URLError):
            pass
    frame = stream_ui._avatar_preview_frame()
    if frame is None:
        raise HTTPException(404, "Chưa có preview.")
    import cv2
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
    if not ok:
        raise HTTPException(500, "Không encode được preview.")
    return Response(content=buf.tobytes(), media_type="image/jpeg")


@router.post("/{session_id}/enqueue", response_model=LivestreamEnqueueResponse)
async def enqueue_audio(
    session_id: str,
    audio: UploadFile,
    request_id: str = Form(""),
    priority: int = Form(0),
    interrupt: bool = Form(False),
):
    import time
    audio_dir = job_files.UPLOADS_DIR / "livestream"
    audio_dir.mkdir(parents=True, exist_ok=True)
    dst = audio_dir / f"{__import__('uuid').uuid4().hex[:12]}_{audio.filename}"
    dst.write_bytes(await audio.read())
    rid = request_id.strip() or f"sentence-{int(time.time())}"
    status = _call(stream_ui._request, "POST", f"/api/streams/{session_id.strip()}/enqueue", {
        "request_id": rid, "audio_path": str(dst.resolve()),
        "priority": int(priority), "interrupt": bool(interrupt),
    })
    return LivestreamEnqueueResponse(status=status, message="Đã enqueue audio")


@router.post("/{session_id}/enqueue-sample", response_model=LivestreamEnqueueResponse)
def enqueue_sample_audio(session_id: str, body: LivestreamEnqueueSampleRequest):
    import time
    if not body.sample_path or not Path(body.sample_path).is_file():
        raise HTTPException(422, "Không tìm thấy file voice mẫu.")
    rid = body.request_id.strip() or f"sample-{int(time.time())}"
    status = _call(stream_ui._request, "POST", f"/api/streams/{session_id.strip()}/enqueue", {
        "request_id": rid, "audio_path": body.sample_path,
        "priority": body.priority, "interrupt": body.interrupt,
    })
    return LivestreamEnqueueResponse(status=status, message="Đã gửi voice mẫu vào render")


@router.post("/{session_id}/interrupt", response_model=dict)
def interrupt_stream(session_id: str):
    return _call(stream_ui._request, "POST", f"/api/streams/{session_id.strip()}/interrupt", {})


@router.delete("/{session_id}", response_model=dict)
def stop_stream(session_id: str):
    return _call(stream_ui._request, "DELETE", f"/api/streams/{session_id.strip()}")
