"""Adapter between the render queue and persistent MuseTalk 1.5 server."""
import contextlib
import fcntl
import hashlib
import json
import os
import select
import signal
import socket
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MUSETALK_ROOT = ROOT / "engines" / "MuseTalk"
MUSETALK_PYTHON = MUSETALK_ROOT / ".venv" / "bin" / "python"
SERVER_SCRIPT = ROOT / "musetalk_render_server.py"
MODEL_ROOT = MUSETALK_ROOT / "models"
SOCKET_PATH = ROOT / ".musetalk.sock"
SERVER_START_LOCK = ROOT / ".musetalk_start.lock"
SERVER_LOG = ROOT / "logs" / "musetalk_server.log"
# MuseTalk renders hold ACTIVE_LOCK shared; a LatentSync render on a low-VRAM card holds it
# exclusive so it can stop the server and get its VRAM back. GATE_LOCK gives the pauser
# priority: while it's held exclusive, new MuseTalk renders queue up instead of streaming in
# behind each other and starving the LatentSync render forever.
ACTIVE_LOCK = ROOT / ".musetalk_active.lock"
GATE_LOCK = ROOT / ".musetalk_gate.lock"
LOCK_HEARTBEAT_SECONDS = 30
REQUIRED_FILES = (
    SERVER_SCRIPT,
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
HEARTBEAT_SECONDS = float(os.environ.get("MUSETALK_HEARTBEAT_SECONDS", "30"))
MAX_RENDER_SECONDS = float(os.environ.get("MUSETALK_MAX_RENDER_SECONDS", str(2 * 60 * 60)))


def _validate_install():
    missing = [str(x) for x in REQUIRED_FILES if not x.is_file()]
    if not MUSETALK_PYTHON.is_file():
        missing.insert(0, str(MUSETALK_PYTHON.relative_to(MUSETALK_ROOT)))
    if missing:
        raise RuntimeError("MuseTalk is missing:\n  - " + "\n  - ".join(missing))


def _video_cache_key(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return f"video_{digest.hexdigest()[:20]}"


def _connect(timeout=2):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    client.connect(str(SOCKET_PATH))
    return client


def _ensure_server():
    """Start the render server unless one is already listening.

    queue_worker runs a matching pair of jobs concurrently, so two render_job
    processes reach this within milliseconds of each other. Without the lock both
    see a dead socket and both spawn a server; the second one rebinds the socket
    and the first is orphaned, holding ~5.5GB of VRAM forever with no way to
    receive work. The lock makes the check-then-spawn atomic across processes,
    and the second holder re-checks and finds the server the first one started.
    """
    try:
        client = _connect(); client.close(); return
    except OSError:
        pass
    # Multiple batched render_job processes can arrive together. Serialize the
    # check/start/wait sequence so exactly one of them owns the GPU model server.
    with open(SERVER_START_LOCK, "a+") as start_lock:
        fcntl.flock(start_lock.fileno(), fcntl.LOCK_EX)
        try:
            client = _connect(); client.close(); return
        except OSError:
            pass
        SERVER_LOG.parent.mkdir(parents=True, exist_ok=True)
        log = open(SERVER_LOG, "a", buffering=1)
        subprocess.Popen(
            [str(MUSETALK_PYTHON), str(SERVER_SCRIPT)],
            cwd=MUSETALK_ROOT, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True,
            env={**os.environ, "MUSETALK_SOCKET": str(SOCKET_PATH)},
        )
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            time.sleep(1)
            try:
                client = _connect(); client.close(); return
            except OSError:
                continue
        raise RuntimeError(f"MuseTalk server did not start; see {SERVER_LOG}")


def _kill_server(timeout=5, reason="wedged"):
    """Kill any running persistent server so the next render spawns a fresh one.

    A CUDA error response means the server's CUDA context is wedged — it still
    accepts connections and will keep failing every request the same way, so
    reconnecting (what _ensure_server does on a live socket) never recovers.
    Matches by SERVER_SCRIPT's path in /proc/*/cmdline since the server has no
    pidfile of its own. Waits for the kill to actually take so a retry landing
    immediately after doesn't race main()'s _socket_is_live() check and exit
    into a server that looks started but isn't.
    """
    script = str(SERVER_SCRIPT)
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = entry.joinpath("cmdline").read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
        if script in cmdline:
            pids.append(int(entry.name))
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"[musetalk] killed {reason} server pid={pid}", flush=True)
        except OSError:
            pass
    deadline = time.monotonic() + timeout
    for pid in pids:
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.2)
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()


def _wait_for_response(client, heartbeat_seconds=HEARTBEAT_SECONDS,
                       max_render_seconds=MAX_RENDER_SECONDS):
    """Wait for one JSON line while keeping the queue watchdog informed.

    MuseTalk writes detailed progress to its own server log, so without this
    heartbeat the parent worker mistakes a healthy long render for a stall.
    """
    started = time.monotonic()
    payload = bytearray()
    client.setblocking(False)
    while True:
        elapsed = time.monotonic() - started
        remaining = max_render_seconds - elapsed
        if remaining <= 0:
            raise TimeoutError(
                f"MuseTalk render exceeded {max_render_seconds / 3600:g} hours"
            )
        ready, _, _ = select.select(
            [client], [], [], min(heartbeat_seconds, remaining)
        )
        if not ready:
            print(f"[musetalk] still rendering elapsed={int(elapsed + heartbeat_seconds)}s",
                  flush=True)
            continue
        chunk = client.recv(64 * 1024)
        if not chunk:
            raise RuntimeError("MuseTalk server disconnected without a response")
        payload.extend(chunk)
        if b"\n" in payload:
            return bytes(payload).split(b"\n", 1)[0].decode("utf-8")


def _flock_with_heartbeat(handle, mode, waiting_msg):
    """flock that prints a line every LOCK_HEARTBEAT_SECONDS while blocked, so the queue
    worker's no-log-output stall check doesn't kill a render that is only waiting its turn."""
    try:
        fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)
        return
    except BlockingIOError:
        pass
    print(waiting_msg, flush=True)
    waited = 0.0
    while True:
        try:
            fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            time.sleep(1)
            waited += 1
            if waited % LOCK_HEARTBEAT_SECONDS == 0:
                print(f"{waiting_msg} ({int(waited)}s)", flush=True)


@contextlib.contextmanager
def _render_slot():
    """Shared hold for one MuseTalk render; waits while a LatentSync render has paused MuseTalk."""
    with open(GATE_LOCK, "a+") as gate, open(ACTIVE_LOCK, "a+") as active:
        _flock_with_heartbeat(gate, fcntl.LOCK_SH,
                              "[musetalk] paused for a LatentSync render on this GPU — waiting...")
        _flock_with_heartbeat(active, fcntl.LOCK_SH, "[musetalk] waiting for render slot...")
        fcntl.flock(gate.fileno(), fcntl.LOCK_UN)
        yield


@contextlib.contextmanager
def paused():
    """Stop the MuseTalk server for the duration of the block, freeing its VRAM.

    Blocks new MuseTalk renders, waits for in-flight ones to finish, then kills the server.
    The next MuseTalk render restarts it via _ensure_server(). Meant for LatentSync on
    low-VRAM cards, where both models don't fit at once.
    """
    with open(GATE_LOCK, "a+") as gate, open(ACTIVE_LOCK, "a+") as active:
        _flock_with_heartbeat(gate, fcntl.LOCK_EX, "[musetalk] waiting to pause MuseTalk...")
        _flock_with_heartbeat(active, fcntl.LOCK_EX,
                              "[musetalk] waiting for running MuseTalk renders to finish...")
        try:
            _kill_server(reason="idle")
            print("[musetalk] server stopped to free VRAM; restarts on the next MuseTalk job",
                  flush=True)
            yield
        finally:
            fcntl.flock(active.fileno(), fcntl.LOCK_UN)
            fcntl.flock(gate.fileno(), fcntl.LOCK_UN)


def render(video_path, audio_path, output_path, work_dir, batch_size=20):
    with _render_slot():
        _render(video_path, audio_path, output_path, work_dir, batch_size)


def _render(video_path, audio_path, output_path, work_dir, batch_size=20):
    _validate_install()
    video_path = Path(video_path).resolve()
    avatar_id = f"{_video_cache_key(video_path)}_b0"
    print(f"[musetalk] persistent server avatar={avatar_id} batch={batch_size}", flush=True)
    _ensure_server()
    request = {
        # Stable across retries. The server uses it to reconnect to an in-flight
        # render instead of submitting the same expensive GPU work again.
        "request_id": str(Path(output_path).resolve()),
        "avatar_id": avatar_id,
        "video_path": str(video_path),
        "audio_path": str(Path(audio_path).resolve()),
        "output_path": str(Path(output_path).resolve()),
        "batch_size": int(batch_size), "bbox_shift": 0, "fps": 25,
    }
    client = _connect(timeout=30)
    with client:
        client.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
        line = _wait_for_response(client)
    response = json.loads(line)
    if not response.get("ok"):
        error = response.get("error", "unknown error")
        if "cuda error" in error.lower():
            print(f"[musetalk] CUDA error from persistent server; restarting it: {error}", flush=True)
            _kill_server()
        raise RuntimeError(f"MuseTalk server error: {error}")
    if not Path(output_path).is_file():
        raise RuntimeError(f"MuseTalk server output missing: {output_path}")
