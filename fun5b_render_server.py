"""Persistent Fun-5B (Wan2.2-Fun-5B-Control) render server for the rendervideo queue.

Runs in engines/Fun5B/.venv with cwd = the app root, spawned by fun5b_adapter._ensure_server()
(never by the main venv: fun5b/ monkeypatches VideoX-Fun and torch at import). Keeps the video
model, the pose detector and the matting model resident between jobs; it is stopped, not
unloaded, when another engine needs the GPU (fun5b_adapter.paused()).

Protocol over a UNIX socket, one JSON object per line:
  -> {"op": "status"}                      <- {"ok": true, "loaded": .., "busy": .., "vram_mb": ..}
  -> {"op": "render", "job_id": .., "image": .., "video": .., "out": .., "preset": "natural",
      "seed": 42, "keep_background": true, "auto_background": true, "background": null,
      "table_line": null, "sharp_hands": false}
  <- {"event": "progress", "stage": "load|plate|pose|render", "progress": 0.42, "message": ".."}  (many)
  <- {"event": "done", "frames": 901}  or  {"event": "error", "message": "..", "retryable": false}
A render is cancelled when its client sends {"op": "cancel"} or simply disconnects (render_job.py
killed by queue_worker on a user cancel), checked after every denoising step.
"""
import ctypes
import gc
import json
import math
import os
import socket
import sys
import threading
import time
import traceback
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_ROOT))
SOCKET_PATH = Path(os.environ.get("FUN5B_SOCKET", str(APP_ROOT / ".fun5b.sock")))

import numpy as np  # noqa: E402
import torch  # noqa: E402
from PIL import Image, ImageOps  # noqa: E402

from fun5b import plate as clean_plate  # noqa: E402
from fun5b.matting import Matting  # noqa: E402
from fun5b.run import (CHUNK, KNOWN, PRESET_KEYS, PRESETS, RENDER_H, RENDER_W, SLOW_MOTION, Cancelled,  # noqa: E402
                       PoseDetector, driving_poses, load_pipeline, render)

_LIBC = ctypes.CDLL("libc.so.6")
STATE = {"pipe": None, "detector": None, "matting": None, "poses": None}
RENDER_LOCK = threading.Lock()  # one render at a time (fun5b's matting and temp files are per process)
BUSY = {"job_id": None}


def _log(msg):
    print(f"[fun5b-server] {msg}", flush=True)


def _release():
    gc.collect()
    torch.cuda.empty_cache()
    _LIBC.malloc_trim(0)  # hand freed frame buffers back to the OS (RAM grew GBs per job without it)


def _load(emit):
    if STATE["pipe"] is None:
        emit("load", 0.0, "Đang nạp mô hình Fun-5B (~2 phút)…")
        tic = time.time()
        STATE["pipe"] = load_pipeline()
        _log(f"video model loaded in {time.time() - tic:.0f}s")
    if STATE["detector"] is None:
        STATE["detector"] = PoseDetector()
        STATE["poses"] = driving_poses(STATE["detector"])
    if STATE["matting"] is None:
        STATE["matting"] = Matting()


def _background_for(reference_np, source_name, emit, cancelled):
    """Clean plate of the fitted reference: from fun5b_cache/plates when this picture was seen
    before, else painted by FireRed (~6.5 min). FireRed needs ~19GB, so the video model is dropped
    meanwhile and loaded again afterwards (~2 min) — as temo's UI did."""
    raw = clean_plate.cached(reference_np)
    if raw is None:
        emit("plate", 0.0, "Đang vẽ nền sạch bằng FireRed (lần đầu với ảnh này, ~7 phút)…")
        STATE["pipe"] = None
        _release()
        try:
            raw = clean_plate.make(reference_np, cancel=cancelled, source_name=source_name)
        finally:
            _release()
            emit("load", 0.0, "Đang nạp lại mô hình video…")
            STATE["pipe"] = load_pipeline()
    else:
        emit("plate", 0.0, "Dùng lại nền sạch đã lưu")
    return Image.fromarray(clean_plate.compose(reference_np, raw, STATE["matting"]))


def _chunks(generated):
    """Chunks render() splits `generated` frames into: the first CHUNK, then CHUNK - KNOWN new each."""
    return 1 if generated <= CHUNK else 1 + math.ceil((generated - CHUNK) / (CHUNK - KNOWN))


def _render(req, emit, cancelled):
    preset_key = req.get("preset") or "natural"
    if preset_key not in PRESET_KEYS:
        raise ValueError(f"preset không hợp lệ: {preset_key!r}")
    preset = PRESET_KEYS[preset_key]
    steps = PRESETS[preset][1]
    image = Image.open(req["image"]).convert("RGB")
    video, out = req["video"], req["out"]
    keep_background = bool(req.get("keep_background", True))
    background = Image.open(req["background"]).convert("RGB") if req.get("background") else None

    _load(emit)
    if keep_background and background is None and req.get("auto_background", True):
        if not clean_plate.available():
            emit("plate", 0.0, "Chưa cài FireRed: dùng LaMa để xoá người khỏi nền")
        else:
            reference_np = np.asarray(ImageOps.fit(image, (RENDER_W, RENDER_H), Image.LANCZOS))
            try:
                background = _background_for(reference_np, Path(req["image"]).name, emit, cancelled)
            except clean_plate.EditCancelled:
                raise Cancelled from None
            except Exception as e:  # noqa: BLE001 - same fallback as temo's UI: LaMa paints the person out
                _log(f"clean plate failed, using LaMa: {e!r}")
                emit("plate", 0.0, f"Vẽ nền sạch lỗi, dùng LaMa: {e}")
    if cancelled():
        raise Cancelled

    # Progress by denoising step: render() calls cancel() once per step, and its "pose:" log line
    # gives the generated frame count, hence the chunk count.
    counter = {"steps": 0, "total": None}

    def log(message):
        _log(message)
        if message.startswith("pose:"):
            generated = int(message.split("(")[1].split()[0])
            counter["total"] = _chunks(generated) * steps
            emit("render", 0.0, "Đang render…")

    def cancel_and_count():
        if cancelled():
            return True
        counter["steps"] += 1
        if counter["total"]:
            emit("render", min(counter["steps"] / counter["total"], 0.999), None)
        return False

    emit("pose", 0.0, "Đang tách pose từ video mẫu…")
    try:
        return render(STATE["pipe"], STATE["detector"], image, video, out, preset, int(req.get("seed", 42)),
                      log=log, cancel=cancel_and_count,
                      matting=STATE["matting"] if keep_background else None,
                      table_line=req.get("table_line"), poses_cache=STATE["poses"],
                      background=background if keep_background else None,
                      slow_motion=SLOW_MOTION if req.get("sharp_hands") else None)
    finally:
        _release()


def _send(conn, obj):
    conn.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))


def _handle_render(conn, req, rest):
    cancel = threading.Event()

    def watch():  # a "cancel" line or the client going away (render_job killed) cancels the render
        buf = rest
        try:
            while not cancel.is_set():
                if b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if json.loads(line or b"{}").get("op") == "cancel":
                        break
                    continue
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buf += chunk
        except (OSError, ValueError):
            pass
        cancel.set()

    last = {"t": 0.0, "stage": None}

    def emit(stage, progress, message):
        now = time.monotonic()
        if message is None and stage == last["stage"] and now - last["t"] < 3:
            return  # step progress: at most one line every 3s
        last.update(t=now, stage=stage)
        try:
            _send(conn, {"event": "progress", "stage": stage, "progress": round(progress, 4), "message": message})
        except OSError:
            cancel.set()

    if not RENDER_LOCK.acquire(blocking=False):
        _send(conn, {"event": "error", "message": f"server đang render job #{BUSY['job_id']}", "retryable": True})
        return
    threading.Thread(target=watch, daemon=True).start()
    BUSY["job_id"] = req.get("job_id")
    tic = time.time()
    try:
        _log(f"job #{req.get('job_id')} start preset={req.get('preset')} keep_bg={req.get('keep_background')}")
        frames = _render(req, emit, cancel.is_set)
        _log(f"job #{req.get('job_id')} done: {frames} frames in {time.time() - tic:.0f}s")
        _try_send(conn, {"event": "done", "frames": frames})
    except Cancelled:
        _log(f"job #{req.get('job_id')} cancelled after {time.time() - tic:.0f}s")
        _try_send(conn, {"event": "error", "message": "cancelled", "cancelled": True, "retryable": False})
    except ValueError as e:  # no person in the image/video, bad preset: retrying cannot help
        _log(f"job #{req.get('job_id')} rejected: {e}")
        _try_send(conn, {"event": "error", "message": str(e), "retryable": False})
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        message = f"{type(e).__name__}: {e}"
        _try_send(conn, {"event": "error", "message": message, "retryable": True,
                         "cuda": isinstance(e, torch.OutOfMemoryError) or "CUDA" in message})
    finally:
        BUSY["job_id"] = None
        RENDER_LOCK.release()


def _try_send(conn, obj):
    try:
        _send(conn, obj)
    except OSError:
        pass


def _handle(conn):
    with conn:
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(65536)
            if not chunk:
                return
            buf += chunk
        line, rest = buf.split(b"\n", 1)
        try:
            req = json.loads(line)
        except ValueError:
            _try_send(conn, {"ok": False, "error": "bad json"})
            return
        op = req.get("op")
        if op == "status":
            free, total = torch.cuda.mem_get_info() if torch.cuda.is_available() else (0, 0)
            _try_send(conn, {"ok": True, "loaded": STATE["pipe"] is not None, "busy": BUSY["job_id"] is not None,
                             "job_id": BUSY["job_id"], "pid": os.getpid(),
                             "vram_mb": round(torch.cuda.memory_reserved() / 2**20),
                             "gpu_free_mb": round(free / 2**20), "gpu_total_mb": round(total / 2**20)})
        elif op == "render":
            _handle_render(conn, req, rest)
        else:
            _try_send(conn, {"ok": False, "error": f"unknown op {op!r}"})


def _socket_is_live():
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(str(SOCKET_PATH))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def main():
    if _socket_is_live():  # another server already owns the GPU models: leave it alone
        _log(f"already running on {SOCKET_PATH}; exiting")
        return
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()
    # Load before listening: the adapter's start-up wait then covers the ~2 min model load, and
    # a status probe never reports a server that cannot render yet.
    _load(lambda *a: None)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(SOCKET_PATH))
    server.listen(8)
    _log(f"listening {SOCKET_PATH} pid={os.getpid()}")
    while True:
        conn, _ = server.accept()
        threading.Thread(target=_handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()
