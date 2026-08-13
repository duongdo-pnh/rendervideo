"""Gradio callbacks for user-managed MuseTalk livestream sessions.

Credentials are accepted from UI inputs and forwarded to the local streaming
backend. They are never persisted to .env/database or written to logs.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import gradio as gr
import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent
BACKEND_HOST = os.environ.get("MUSETALK_STREAM_HOST", "127.0.0.1")
BACKEND_PORT = int(os.environ.get("MUSETALK_STREAM_PORT", "8091"))
BACKEND_URL = f"http://{BACKEND_HOST}:{BACKEND_PORT}"
MUSETALK_PYTHON = ROOT / "engines" / "MuseTalk" / ".venv" / "bin" / "python"
BACKEND_SCRIPT = ROOT / "musetalk_stream_api.py"
BACKEND_LOG = ROOT / "logs" / "musetalk_stream_api.log"
FACEBOOK_AVATAR_720 = Path(
    os.environ.get(
        "FACEBOOK_LIVE_AVATAR_PATH",
        str(ROOT / "avatar_cache" / "facebook_live_selected_720x1280.mp4"),
    )
)
# Tên file gốc trước khi resize/ghi đè lên FACEBOOK_AVATAR_720 — ghi lại vì "Start stream" luôn
# GHI ĐÈ cùng 1 đường dẫn cố định nên tên upload gốc bị mất ngay khi convert xong.
FACEBOOK_AVATAR_SOURCE_NAME = FACEBOOK_AVATAR_720.with_suffix(".source.txt")
SAMPLE_AUDIO_DIR = ROOT / "engines" / "MuseTalk" / "data" / "audio"
_CURRENT_PREVIEW_SESSION = "facebook-live"
_CURRENT_AVATAR_VIDEO = None


def _sample_audio_choices() -> list[tuple[str, str]]:
    samples = [
        SAMPLE_AUDIO_DIR / "eng.wav",
        SAMPLE_AUDIO_DIR / "sun.wav",
        SAMPLE_AUDIO_DIR / "yongen.wav",
    ]
    choices: list[tuple[str, str]] = []
    for path in samples:
        if path.is_file():
            label = path.stem.replace("_", " ")
            choices.append((f"{label} · {path.name}", str(path.resolve())))
    return choices


def _backend_ready(timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((BACKEND_HOST, BACKEND_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def _ensure_backend() -> None:
    if _backend_ready():
        return
    if not MUSETALK_PYTHON.is_file():
        raise gr.Error("Chưa cài MuseTalk environment. Xem docs/MUSETALK_STREAMING_USAGE.md.")
    BACKEND_LOG.parent.mkdir(parents=True, exist_ok=True)
    log = open(BACKEND_LOG, "a", buffering=1)
    subprocess.Popen(
        [
            str(MUSETALK_PYTHON), str(BACKEND_SCRIPT),
            "--host", BACKEND_HOST, "--port", str(BACKEND_PORT),
        ],
        cwd=ROOT,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
    )
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        if _backend_ready():
            return
        time.sleep(1)
    raise gr.Error(f"Streaming backend không khởi động được. Xem {BACKEND_LOG}.")


def _request(method: str, path: str, body: dict | None = None, timeout: float = 180) -> dict:
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        BACKEND_URL + path,
        data=payload,
        headers={"Content-Type": "application/json"} if payload else {},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read()).get("error", f"HTTP {exc.code}")
        except Exception:
            message = f"HTTP {exc.code}"
        raise gr.Error(f"Streaming API: {message}") from None
    except (OSError, urllib.error.URLError) as exc:
        raise gr.Error(f"Không kết nối được streaming backend: {type(exc).__name__}") from None


def _compose_push_url(server_url: str, stream_key: str) -> str:
    server_url = (server_url or "").strip()
    stream_key = (stream_key or "").strip()
    if not server_url:
        raise gr.Error("Cần nhập Facebook Server URL.")
    if not stream_key:
        raise gr.Error("Cần nhập Facebook Stream key.")
    if any(char.isspace() for char in stream_key):
        raise gr.Error("Stream key không được chứa khoảng trắng.")
    parts = urlsplit(server_url)
    if parts.scheme not in {"rtmp", "rtmps"} or not parts.hostname:
        raise gr.Error("Server URL phải bắt đầu bằng rtmp:// hoặc rtmps://.")
    # Credentials in the authority are unnecessary and easy to leak.
    if parts.username or parts.password:
        raise gr.Error("Không đặt username/password trong Server URL.")
    base_path = parts.path.rstrip("/")
    push_path = f"{base_path}/{stream_key.lstrip('/')}"
    return urlunsplit((parts.scheme, parts.netloc, push_path, "", ""))


def _normalize_facebook_avatar(avatar_video: str) -> str:
    """Convert the complete uploaded avatar to a stable portrait 720p/30fps file."""
    if not avatar_video:
        raise gr.Error("Cần tải lên avatar video hợp lệ.")
    source = Path(avatar_video).resolve()
    if not source.is_file():
        raise gr.Error("Cần tải lên avatar video hợp lệ.")
    FACEBOOK_AVATAR_720.parent.mkdir(parents=True, exist_ok=True)
    temporary = FACEBOOK_AVATAR_720.with_name(
        f".{FACEBOOK_AVATAR_720.stem}.{os.getpid()}.tmp.mp4"
    )
    command_prefix = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source),
        "-vf",
        "scale=720:1280:force_original_aspect_ratio=decrease,"
        "pad=720:1280:(ow-iw)/2:(oh-ih)/2:color=black,fps=30",
        "-an",
    ]
    nvenc_command = command_prefix + [
        "-c:v", "h264_nvenc", "-preset", "p4", "-cq", "20", "-b:v", "0",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temporary),
    ]
    cpu_command = command_prefix + [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temporary),
    ]
    try:
        try:
            subprocess.run(nvenc_command, check=True, timeout=1800)
        except subprocess.CalledProcessError:
            subprocess.run(cpu_command, check=True, timeout=1800)
        os.replace(temporary, FACEBOOK_AVATAR_720)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        temporary.unlink(missing_ok=True)
        raise gr.Error(f"Không thể resize avatar về 720×1280: {type(exc).__name__}") from None
    try:
        FACEBOOK_AVATAR_SOURCE_NAME.write_text(source.name, encoding="utf-8")
    except OSError:
        pass  # best-effort; chỉ phục vụ hiển thị tên gốc ở tab Avatar Cache
    return str(FACEBOOK_AVATAR_720.resolve())


def _status_text(status: dict, prefix: str = "") -> str:
    label = prefix or "Trạng thái"
    progress = float(status.get("audio_progress_percent", 0) or 0)
    filled = min(20, max(0, int(progress / 5)))
    bar = "█" * filled + "░" * (20 - filled)
    return (
        f"### {label}: `{status.get('status', 'unknown')}`\n"
        f"- Session: `{status.get('session_id', '-')}`\n"
        f"- Khung hình: **{status.get('width', '-')}×{status.get('height', '-')}** "
        "(theo video avatar)\n"
        f"- Output FPS: **{status.get('output_fps', 0)}**\n"
        f"- OBS FPS target: **{status.get('output_fps_target', status.get('fps', '-'))}**\n"
        f"- Render FPS: **{status.get('render_fps', 0)}**\n"
        f"- AV drift: **{status.get('av_drift_ms', 0)} ms**\n"
        f"- Audio delay: **{status.get('audio_delay_ms', 0)} ms**\n"
        f"- Buffer: **{status.get('video_buffer_frames', 0)} frame / "
        f"{round(float(status.get('audio_buffer_ms', 0) or 0) / 1000, 2)} giây**\n"
        f"- Output: **{status.get('transport', 'rtmp')}** "
        f"{('đã kết nối' if status.get('transport_running') else '')}\n"
        f"- Reconnect: **{status.get('reconnect_count', 0)}**\n"
        f"- Lỗi transport: `{status.get('transport_last_error') or '-'}`\n"
        f"- Request hiện tại: `{status.get('current_request_id') or '-'}`\n"
        f"- Tiến trình audio: **{bar} {progress:.1f}%** "
        f"({status.get('audio_played_seconds', 0)} / {status.get('audio_total_seconds', 0)} giây)\n"
        f"- Render audio: **{status.get('audio_rendered_frames', 0)} / {status.get('audio_total_frames', 0)} frame**\n"
        f"- Câu đang chờ: **{status.get('sentence_queue_size', 0)}**"
    )


_PREP_STAGE_LABELS = {
    "starting": "Đang khởi tạo",
    "frames": "Đang trích khung hình từ video mẫu",
    "landmark": "Đang nhận diện khuôn mặt",
    "mask_latent": "Đang tạo mask và latent",
}


def _cache_progress_text(percent: int, label: str) -> str:
    percent = min(100, max(0, int(percent)))
    filled = min(20, percent // 5)
    bar = "█" * filled + "░" * (20 - filled)
    return (
        "#### Tiến trình tạo cache\n"
        f"`{bar}` **{percent}%**  \n"
        f"{label}"
    )


def start_stream(
    session_id: str,
    avatar_video: str,
    server_url: str,
    stream_key: str,
    render_ahead_seconds: float,
    batch_size: int,
    obs_output_fps: int,
    audio_delay_ms: int,
    avatar_max_seconds: float = 0,
):
    global _CURRENT_PREVIEW_SESSION, _CURRENT_AVATAR_VIDEO
    session_id = (session_id or "").strip()
    if not session_id:
        raise gr.Error("Cần nhập Session ID.")
    yield (
        "### Đang chuẩn bị mẫu...",
        {"cache_progress_percent": 2, "cache_stage": "checking"},
        gr.update(),
        _cache_progress_text(2, "Đang kiểm tra video mẫu..."),
    )
    if session_id == "facebook-live":
        avatar_video = _normalize_facebook_avatar(avatar_video)
        yield (
            "### Đã resize mẫu, chuẩn bị tạo cache...",
            {"cache_progress_percent": 35, "cache_stage": "resized"},
            gr.update(),
            _cache_progress_text(35, "Resize 720×1280, 30 FPS đã hoàn tất."),
        )
    _CURRENT_PREVIEW_SESSION = session_id
    _CURRENT_AVATAR_VIDEO = str(Path(avatar_video).resolve()) if avatar_video else None
    if not avatar_video or not Path(avatar_video).is_file():
        raise gr.Error("Cần tải lên avatar video hợp lệ.")
    use_obs_udp = (
        session_id == "facebook-live"
        and not (server_url or "").strip()
        and not (stream_key or "").strip()
    )
    use_relive = (
        not use_obs_udp
        and not (server_url or "").strip()
        and not (stream_key or "").strip()
    )
    push_url = (
        "udp://127.0.0.1:5000?pkt_size=1316"
        if use_obs_udp
        else ("" if use_relive else _compose_push_url(server_url, stream_key))
    )
    _ensure_backend()
    yield (
        "### Đang tạo cache MuseTalk...",
        {"cache_progress_percent": 45, "cache_stage": "musetalk_cache"},
        gr.update(),
        _cache_progress_text(
            45, "Đang nhận diện khuôn mặt, tạo latent và mask. Vui lòng chờ..."
        ),
    )
    status = _request("POST", "/api/streams", {
        "session_id": session_id,
        "avatar_id": session_id,
        "avatar_video": str(Path(avatar_video).resolve()),
        "push_url": push_url,
        "output_mode": "udp" if use_obs_udp else ("relive" if use_relive else "rtmp"),
        "fps": 30,
        "output_fps": int(obs_output_fps) if obs_output_fps else 30,
        "warmup_frames": (
            30 if session_id == "facebook-live"
            else min(250, max(0, int(round(float(render_ahead_seconds) * 30))))
        ),
        "batch_size": int(batch_size),
        "audio_delay_ms": int(audio_delay_ms),
        "avatar_max_seconds": float(avatar_max_seconds) if avatar_max_seconds else 0,
    }, timeout=30)
    # The backend now answers immediately and preps the avatar (~40 min for a
    # fresh clip) in a background thread — poll its real progress instead of
    # holding one HTTP request open that long. A single blocking call used to
    # outlive Gradio's own request timeout and surface a scary "Timeout" error
    # even though the build kept running fine on the server.
    # live_cache_progress is the one progress readout — no gr.Progress() bar
    # alongside it repeating the same stage/percent/elapsed text.
    prep_deadline = time.monotonic() + 3600
    while status.get("status") == "preparing":
        percent = float(status.get("prep_percent") or 0.0)
        stage = _PREP_STAGE_LABELS.get(status.get("prep_stage"), status.get("prep_stage") or "")
        detail = status.get("prep_detail") or ""
        elapsed = int(status.get("elapsed_seconds") or 0)
        yield (
            "### Đang chuẩn bị avatar...",
            status,
            gr.update(),
            _cache_progress_text(int(45 + 50 * percent / 100), f"{stage} {detail} (đã chạy {elapsed}s)"),
        )
        if time.monotonic() > prep_deadline:
            raise gr.Error("Chuẩn bị avatar quá lâu (>60 phút). Kiểm tra logs/musetalk_stream_api.log.")
        time.sleep(3)
        status = _request("GET", f"/api/streams/{session_id}", timeout=10)
    if status.get("status") == "error":
        raise gr.Error(f"Chuẩn bị avatar lỗi: {status.get('error')}")
    # Clear the password field after the backend has accepted the credential.
    yield (
        _status_text(status, "Đã bắt đầu stream"),
        status,
        gr.update(value=""),
        _cache_progress_text(100, "Hoàn tất cache và đã nối luồng với OBS."),
    )


def enqueue_audio(
    session_id: str,
    request_id: str,
    audio_path: str,
    priority: int,
    interrupt: bool,
):
    if not audio_path or not Path(audio_path).is_file():
        raise gr.Error("Cần tải lên audio hợp lệ.")
    request_id = (request_id or "").strip() or f"sentence-{int(time.time())}"
    status = _request(
        "POST", f"/api/streams/{(session_id or '').strip()}/enqueue",
        {
            "request_id": request_id,
            "audio_path": str(Path(audio_path).resolve()),
            "priority": int(priority),
            "interrupt": bool(interrupt),
        },
    )
    return _status_text(status, "Đã enqueue audio"), status


def enqueue_audio_playlist(session_id: str, audio_paths):
    """Enqueue nhiều file voice theo thứ tự người dùng chọn cho MuseTalk realtime."""
    paths = [str(Path(path).resolve()) for path in (audio_paths or []) if path]
    if not paths:
        raise gr.Error("Chọn ít nhất một file audio cho Voice playlist.")
    for path in paths:
        if not Path(path).is_file():
            raise gr.Error(f"Không tìm thấy audio: {Path(path).name}")
    status = None
    stamp = int(time.time())
    for index, path in enumerate(paths, 1):
        status = _request(
            "POST", f"/api/streams/{(session_id or '').strip()}/enqueue",
            {
                "request_id": f"voice-playlist-{stamp}-{index}",
                "audio_path": path,
                "priority": 10,
                "interrupt": False,
            },
        )
    return _status_text(status, f"Đã thêm {len(paths)} voice vào playlist"), status


def enqueue_sample_audio(session_id: str, sample_audio: str, request_id: str, priority: int, interrupt: bool):
    sample_audio = (sample_audio or "").strip()
    if not sample_audio:
        raise gr.Error("Chọn một voice mẫu trước khi gửi.")
    if not Path(sample_audio).is_file():
        raise gr.Error("Không tìm thấy file voice mẫu.")
    request_id = (request_id or "").strip() or f"sample-{int(time.time())}"
    status = _request(
        "POST", f"/api/streams/{(session_id or '').strip()}/enqueue",
        {
            "request_id": request_id,
            "audio_path": sample_audio,
            "priority": int(priority),
            "interrupt": bool(interrupt),
        },
    )
    return _status_text(status, "Đã gửi voice mẫu vào render"), status


def refresh_stream(session_id: str):
    status = _request("GET", f"/api/streams/{(session_id or '').strip()}")
    return _status_text(status), status


def preview_stream():
    session_id = _CURRENT_PREVIEW_SESSION
    image = _avatar_preview_frame()
    if not session_id or not _backend_ready():
        return image, "### Trạng thái: đang chờ Start stream", {}
    try:
        status = _request("GET", f"/api/streams/{session_id}", timeout=1)
        with urllib.request.urlopen(
            f"{BACKEND_URL}/api/streams/{session_id}/preview.jpg", timeout=2
        ) as response:
            image = np.asarray(Image.open(response).convert("RGB"))
        return image, _status_text(status), status
    except Exception:
        return image, "### Trạng thái: đang chuẩn bị avatar/session...", {}


def _avatar_preview_frame():
    if not _CURRENT_AVATAR_VIDEO or not Path(_CURRENT_AVATAR_VIDEO).is_file():
        return None
    try:
        import cv2
        capture = cv2.VideoCapture(_CURRENT_AVATAR_VIDEO)
        ok, frame = capture.read()
        capture.release()
        if ok:
            return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    except Exception:
        pass
    return None


def interrupt_stream(session_id: str):
    status = _request("POST", f"/api/streams/{(session_id or '').strip()}/interrupt", {})
    return _status_text(status, "Đã ngắt câu"), status


def stop_stream(session_id: str):
    status = _request("DELETE", f"/api/streams/{(session_id or '').strip()}")
    return _status_text(status, "Đã dừng stream"), status
