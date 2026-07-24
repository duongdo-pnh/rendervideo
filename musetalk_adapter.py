"""Adapter between the render queue and the persistent MuseTalk 1.5 server."""
import hashlib
import json
import os
import socket
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MUSETALK_ROOT = ROOT / "engines" / "MuseTalk"
MUSETALK_PYTHON = MUSETALK_ROOT / ".venv" / "bin" / "python"
MODEL_ROOT = MUSETALK_ROOT / "models"
SOCKET_PATH = ROOT / ".musetalk.sock"
SERVER_LOG = ROOT / "logs" / "musetalk_server.log"
REQUIRED_FILES = (
    MODEL_ROOT / "musetalkV15" / "musetalk.json",
    MODEL_ROOT / "musetalkV15" / "unet.pth",
    MODEL_ROOT / "sd-vae" / "config.json",
    MODEL_ROOT / "sd-vae" / "diffusion_pytorch_model.bin",
    MODEL_ROOT / "whisper" / "config.json",
    MODEL_ROOT / "whisper" / "pytorch_model.bin",
    MODEL_ROOT / "whisper" / "preprocessor_config.json",
    MODEL_ROOT / "face-parse-bisent" / "79999_iter.pth",
    MODEL_ROOT / "face-parse-bisent" / "resnet18-5c106cde.pth",
)


def _validate_install():
    missing = [str(path.relative_to(MUSETALK_ROOT)) for path in REQUIRED_FILES if not path.is_file()]
    if not MUSETALK_PYTHON.is_file():
        missing.insert(0, str(MUSETALK_PYTHON.relative_to(MUSETALK_ROOT)))
    if missing:
        raise RuntimeError("MuseTalk is missing:\n  - " + "\n  - ".join(missing))


def _video_cache_key(video_path):
    digest = hashlib.sha256()
    with open(video_path, "rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return f"video_{digest.hexdigest()[:20]}"


def _connect(timeout=2):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    client.connect(str(SOCKET_PATH))
    return client


def _ensure_server():
    try:
        client = _connect()
        client.close()
        return
    except OSError:
        pass
    SERVER_LOG.parent.mkdir(parents=True, exist_ok=True)
    log = open(SERVER_LOG, "a")
    subprocess.Popen(
        [str(MUSETALK_PYTHON), "-m", "scripts.render_server"],
        cwd=MUSETALK_ROOT, stdout=log, stderr=subprocess.STDOUT,
        start_new_session=True, close_fds=True,
    )
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        time.sleep(1)
        try:
            client = _connect()
            client.close()
            return
        except OSError:
            continue
    raise RuntimeError(f"MuseTalk server did not start; see {SERVER_LOG}")


def render(video_path, audio_path, output_path, work_dir, batch_size=20):
    _validate_install()
    video_path = Path(video_path).resolve()
    bbox_shift = -3
    avatar_id = f"{_video_cache_key(video_path)}_b{bbox_shift}"
    print(f"[musetalk] persistent server avatar={avatar_id}", flush=True)
    _ensure_server()
    request = {
        "avatar_id": avatar_id,
        "video_path": str(video_path),
        "audio_path": str(Path(audio_path).resolve()),
        "output_path": str(Path(output_path).resolve()),
        "batch_size": batch_size,
        "bbox_shift": bbox_shift,
    }
    client = _connect(timeout=30 * 60)
    with client:
        client.sendall((json.dumps(request) + "\n").encode())
        response = json.loads(client.makefile("r").readline())
    if not response.get("ok"):
        raise RuntimeError(f"MuseTalk server error: {response.get('error', 'unknown error')}")
    if not Path(output_path).is_file():
        raise RuntimeError(f"MuseTalk server output missing: {output_path}")
