"""Adapter between the render queue and the persistent Fun-5B server (fun5b_render_server.py).

Mirrors musetalk_adapter.py: render_job.py calls render() in-process (main venv); the server runs
in engines/Fun5B/.venv, is started lazily on the first Fun-5B job and stays resident. It is
stopped — never its models deleted — when a MuseTalk/LatentSync job needs the GPU
(ensure_stopped()), and started again by the next Fun-5B job.
"""
import contextlib
import fcntl
import json
import os
import select
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENGINE_ROOT = ROOT / "engines" / "Fun5B"
PYTHON = ENGINE_ROOT / ".venv" / "bin" / "python"
EDIT_PYTHON = ENGINE_ROOT / ".venv-edit" / "bin" / "python"
SERVER_SCRIPT = ROOT / "fun5b_render_server.py"
VIDEOX_DIR = ROOT / "engines" / "VideoX-Fun"
CKPT_DIR = ROOT / "checkpoints" / "fun5b"
CACHE_DIR = ROOT / "fun5b_cache"
PLATE_DIR = CACHE_DIR / "plates"
SOCKET_PATH = ROOT / ".fun5b.sock"
SERVER_START_LOCK = ROOT / ".fun5b_start.lock"
# Fun-5B renders hold ACTIVE_LOCK shared; paused() takes it exclusive to stop the server
# between renders, never during one.
ACTIVE_LOCK = ROOT / ".fun5b_active.lock"
SERVER_LOG = ROOT / "logs" / "fun5b_server.log"
_W = CKPT_DIR / "wan22-fun-5b"
REQUIRED_FILES = (
    SERVER_SCRIPT,
    VIDEOX_DIR / "config" / "wan2.2" / "wan_civitai_5b.yaml",
    _W / "Wan2.2-Fun-5B-Control" / "diffusion_pytorch_model.safetensors",
    _W / "Wan2.2-Fun-5B-Control" / "Wan2.2_VAE.pth",
    _W / "Wan2.2-Fun-5B-Control" / "models_t5_umt5-xxl-enc-bf16.pth",
    _W / "loras" / "FastWan" / "Wan2_2_5B_FastWanFullAttn_lora_rank_128_bf16.safetensors",
    _W / "DWPose" / "yolox_l.onnx",
    _W / "DWPose" / "dw-ll_ucoco_384.onnx",
    _W / "lama" / "big-lama.pt",
    _W / "rvm" / "hub" / "checkpoints" / "rvm_resnet50.pth",
    _W / "rvm" / "hub" / "PeterL1n_RobustVideoMatting_master" / "hubconf.py",
)
FIRERED_GGUF = CKPT_DIR / "firered-image-edit" / "FireRed-Image-Edit-1.1-Q5_K_M.gguf"
MIN_VRAM_GIB = 20  # the VAE decode peaks at ~20GB (fun5b/run.py GpuPipeline.decode_video)
START_TIMEOUT = float(os.environ.get("FUN5B_START_TIMEOUT", "600"))  # model load ~2 min, first compile more
HEARTBEAT_SECONDS = 30
MAX_RENDER_SECONDS = float(os.environ.get("FUN5B_MAX_RENDER_SECONDS", str(6 * 60 * 60)))
MEMORY_MAX = os.environ.get("FUN5B_MEMORY_MAX", "22G")  # as temo's scripts/fun-ui.sh


class RenderRejected(Exception):
    """The job itself is bad (no person found, unreadable file): retrying cannot help."""


class RenderCancelled(Exception):
    pass


def missing_files():
    missing = [str(p.relative_to(ROOT)) for p in REQUIRED_FILES if not p.is_file()]
    if not PYTHON.is_file():
        missing.insert(0, str(PYTHON.relative_to(ROOT)))
    return missing


def unavailable_reason():
    """(reason, missing files): why Fun-5B cannot run here, reason None when it can. Shared by
    the UI (backend/engines.py) and the engines akali_bridge declares when claiming jobs."""
    missing = missing_files()
    if missing:
        return "Chưa cài Fun-5B (thiếu model hoặc môi trường Python).", missing
    import gpu_info

    total = gpu_info.gpu_total_gib()
    if 0 < total < MIN_VRAM_GIB:
        return f"GPU chỉ có {total:.0f} GB VRAM, Fun-5B cần ~{MIN_VRAM_GIB} GB.", []
    return None, []


def plate_available():
    """FireRed (clean background painting) installed: its weights and its own venv."""
    return FIRERED_GGUF.is_file() and EDIT_PYTHON.is_file()


def _connect(timeout=2):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    client.connect(str(SOCKET_PATH))
    return client


def server_status(timeout=2):
    """The server's status dict, or None when it is not running."""
    try:
        with _connect(timeout) as client:
            client.sendall(b'{"op": "status"}\n')
            buf = b""
            while b"\n" not in buf:
                chunk = client.recv(4096)
                if not chunk:
                    return None
                buf += chunk
        return json.loads(buf.split(b"\n", 1)[0])
    except (OSError, ValueError):
        return None


def _server_env():
    env = {k: os.environ[k] for k in ("HOME", "LANG", "LC_ALL", "TZ", "USER", "PATH") if k in os.environ}
    env.update({
        "FUN5B_SOCKET": str(SOCKET_PATH),
        "PYTHONUNBUFFERED": "1",
        "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True",
        # glibc maps every frame-sized buffer on its own: with the adaptive default the freed
        # frames of each job stayed in the heap and the process grew by GBs per job.
        "MALLOC_MMAP_THRESHOLD_": "1048576",
        "TORCHINDUCTOR_COMPILE_THREADS": "1",
        "PATH": f"{PYTHON.parent}{os.pathsep}{env.get('PATH', '/usr/bin:/bin')}",
    })
    return env


def _memory_capped(cmd):
    """Run under a systemd user scope with MemoryMax (an OOM kill then takes only this server,
    never the desktop or the queue worker); plain command where there is no user systemd."""
    runtime = f"/run/user/{os.getuid()}"
    if not shutil.which("systemd-run") or not os.path.isdir(runtime):
        return cmd, {}
    extra = {"XDG_RUNTIME_DIR": runtime, "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus"}
    probe = subprocess.run(["systemd-run", "--user", "--scope", "--quiet", "true"],
                           env={**os.environ, **extra}, capture_output=True, timeout=15)
    if probe.returncode != 0:
        return cmd, {}
    return ["systemd-run", "--user", "--scope", "--quiet", "-p", f"MemoryMax={MEMORY_MAX}",
            "--", *cmd], extra


def _ensure_server():
    """Start the render server unless one is already listening (flock: one spawner at a time)."""
    if server_status() is not None:
        return
    with open(SERVER_START_LOCK, "a+") as start_lock:
        fcntl.flock(start_lock.fileno(), fcntl.LOCK_EX)
        if server_status() is not None:
            return
        _kill_server(reason="stale")
        SERVER_LOG.parent.mkdir(parents=True, exist_ok=True)
        log = open(SERVER_LOG, "a", buffering=1)
        cmd, extra_env = _memory_capped([str(PYTHON), "-u", str(SERVER_SCRIPT)])
        print("[fun5b] starting render server (model load ~2 min)…", flush=True)
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True, close_fds=True, env={**_server_env(), **extra_env})
        started = time.monotonic()
        while time.monotonic() - started < START_TIMEOUT:
            time.sleep(2)
            if server_status() is not None:
                print(f"[fun5b] server ready in {time.monotonic() - started:.0f}s", flush=True)
                return
            if proc.poll() is not None:
                raise RuntimeError(f"Fun-5B server exited (rc={proc.returncode}); see {SERVER_LOG}")
            if int(time.monotonic() - started) % HEARTBEAT_SECONDS < 2:
                print(f"[fun5b] waiting for server… {time.monotonic() - started:.0f}s", flush=True)
        raise RuntimeError(f"Fun-5B server did not start in {START_TIMEOUT:.0f}s; see {SERVER_LOG}")


def _server_pids():
    script = str(SERVER_SCRIPT)
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            argv = entry.joinpath("cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        # The python process itself (argv[0] is the venv python), not a systemd-run wrapper.
        if len(argv) > 2 and argv[0].endswith(b"python") and script.encode() in argv[1:3]:
            pids.append(int(entry.name))
    return pids


def _kill_server(timeout=15, reason="stop"):
    """Stop the server process; models stay on disk, the next Fun-5B job starts it again."""
    pids = _server_pids()
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"[fun5b] stopped {reason} server pid={pid}", flush=True)
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
        else:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
    if SOCKET_PATH.exists() and server_status() is None:
        SOCKET_PATH.unlink(missing_ok=True)
    return bool(pids)


def _flock_with_heartbeat(handle, mode, waiting_msg, nonblocking=False):
    try:
        fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        if nonblocking:
            return False
    print(waiting_msg, flush=True)
    waited = 0
    while True:
        try:
            fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            time.sleep(1)
            waited += 1
            if waited % HEARTBEAT_SECONDS == 0:
                print(f"{waiting_msg} ({waited}s)", flush=True)


def ensure_stopped():
    """Stop the Fun-5B server before another engine renders (it holds ~12GB VRAM / ~14GB RAM).
    Waits for a running Fun-5B render to finish first; no-op when the server is not running.
    The lock is released right away: queue_worker claims one job (or one MuseTalk pair) at a time,
    so no Fun-5B job can start before this engine's render is over, and holding it would serialize
    a concurrent MuseTalk pair."""
    if not _server_pids():
        return
    with open(ACTIVE_LOCK, "a+") as active:
        _flock_with_heartbeat(active, fcntl.LOCK_EX, "[fun5b] waiting for the Fun-5B render to finish…")
        try:
            if _kill_server(reason="paused"):
                print("[fun5b] server stopped to free the GPU; restarts on the next Fun-5B job", flush=True)
        finally:
            fcntl.flock(active.fileno(), fcntl.LOCK_UN)


def stop_server():
    """Stop the server now (UI button). False when a render is running (nothing is stopped)."""
    with open(ACTIVE_LOCK, "a+") as active:
        if not _flock_with_heartbeat(active, fcntl.LOCK_EX, "", nonblocking=True):
            return False
        try:
            _kill_server(reason="manual")
            return True
        finally:
            fcntl.flock(active.fileno(), fcntl.LOCK_UN)


def _read_events(client, on_progress):
    """Yield the server's progress events to on_progress; return the final done/error event."""
    started = time.monotonic()
    buf = b""
    client.setblocking(False)
    while True:
        elapsed = time.monotonic() - started
        if elapsed > MAX_RENDER_SECONDS:
            raise TimeoutError(f"Fun-5B render exceeded {MAX_RENDER_SECONDS / 3600:g} hours")
        ready, _, _ = select.select([client], [], [], HEARTBEAT_SECONDS)
        if not ready:
            print(f"[fun5b] still rendering elapsed={int(elapsed)}s", flush=True)
            continue
        chunk = client.recv(65536)
        if not chunk:
            raise RuntimeError("Fun-5B server disconnected without a result (crashed? see logs/fun5b_server.log)")
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            event = json.loads(line)
            if event.get("event") == "progress":
                on_progress(event)
            else:
                return event


def render(job_id, image_path, video_path, output_path, preset="natural", seed=42, keep_background=True,
           auto_background=True, table_line=None, sharp_hands=False, background_path=None, on_progress=None):
    """Render one Fun-5B job through the persistent server (started if needed); raises
    RenderRejected (bad input, do not retry) / RenderCancelled / RuntimeError (retryable)."""
    missing = missing_files()
    if missing:
        raise RenderRejected("Fun-5B chưa cài đủ:\n  - " + "\n  - ".join(missing))
    with open(ACTIVE_LOCK, "a+") as active:
        _flock_with_heartbeat(active, fcntl.LOCK_SH, "[fun5b] waiting for the server to be free…")
        _ensure_server()
        request = {
            "op": "render", "job_id": job_id,
            "image": str(Path(image_path).resolve()), "video": str(Path(video_path).resolve()),
            "out": str(Path(output_path).resolve()), "preset": preset, "seed": int(seed),
            "keep_background": bool(keep_background), "auto_background": bool(auto_background),
            "table_line": table_line, "sharp_hands": bool(sharp_hands),
            "background": str(Path(background_path).resolve()) if background_path else None,
        }
        last_message = [None]

        def progress(event):
            if event.get("message") and event["message"] != last_message[0]:
                last_message[0] = event["message"]
                print(f"[fun5b] {event['message']}", flush=True)
            if event.get("stage") == "render":
                print(f"[fun5b] render {event.get('progress', 0) * 100:.1f}%", flush=True)
            if on_progress:
                on_progress(event)

        client = _connect(timeout=30)
        with client:
            client.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
            result = _read_events(client, progress)
    if result.get("event") == "done":
        if not Path(output_path).is_file():
            raise RuntimeError(f"Fun-5B server output missing: {output_path}")
        print(f"[fun5b] done: {result.get('frames')} frames", flush=True)
        return result
    message = result.get("message", "unknown error")
    if result.get("cancelled"):
        raise RenderCancelled(message)
    if result.get("cuda"):
        print(f"[fun5b] CUDA error; restarting the server on the next job: {message}", flush=True)
        _kill_server(reason="wedged")
    if result.get("retryable") is False:
        raise RenderRejected(message)
    raise RuntimeError(f"Fun-5B server error: {message}")
