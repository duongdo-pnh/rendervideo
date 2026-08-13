"""Persistent MuseTalk 1.5 render server for the rendervideo queue."""
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
from types import SimpleNamespace

APP_ROOT = Path(__file__).resolve().parent
MUSETALK_ROOT = APP_ROOT / "engines" / "MuseTalk"
sys.path.insert(0, str(MUSETALK_ROOT))

import avatar_cache_gc
import cv2
import torch
from transformers import WhisperModel

import scripts.realtime_inference as rt
from musetalk.utils.audio_processor import AudioProcessor
from musetalk.utils.blending import get_image_blending
from musetalk.utils.face_parsing import FaceParsing
from musetalk.utils.utils import datagen, load_all_model

ROOT = MUSETALK_ROOT
SOCKET_PATH = Path(os.environ.get("MUSETALK_SOCKET", str(APP_ROOT / ".musetalk.sock")))
RUNS_DIR = ROOT / "results" / "server_runs"
GPU_LOCK = threading.Lock()
AVATAR_LOCK = threading.Lock()
AVATARS = {}
REQUESTS = __import__("queue").Queue()
COALESCE_SECONDS = float(os.environ.get("MUSETALK_BATCH_WAIT", "2.0"))
MAX_GROUP_SIZE = int(os.environ.get("MUSETALK_MAX_GROUP", "2"))
MAX_GPU_BATCH = int(os.environ.get("MUSETALK_GPU_BATCH", "32"))


def _load_runtime():
    rt.args = SimpleNamespace(
        version="v15", extra_margin=10, parsing_mode="jaw",
        left_cheek_width=90, right_cheek_width=90,
        audio_padding_length_left=2, audio_padding_length_right=2,
        skip_save_images=False,
    )
    rt.device = torch.device("cuda:0")
    rt.vae, rt.unet, rt.pe = load_all_model(
        unet_model_path="models/musetalkV15/unet.pth",
        vae_type="sd-vae", unet_config="models/musetalkV15/musetalk.json",
        device=rt.device,
    )
    rt.timesteps = torch.tensor([0], device=rt.device)
    rt.pe = rt.pe.half().to(rt.device)
    rt.vae.vae = rt.vae.vae.half().to(rt.device)
    rt.unet.model = rt.unet.model.half().to(rt.device)
    rt.audio_processor = AudioProcessor(feature_extractor_path="models/whisper")
    rt.weight_dtype = rt.unet.model.dtype
    rt.whisper = WhisperModel.from_pretrained("models/whisper")
    rt.whisper = rt.whisper.to(device=rt.device, dtype=rt.weight_dtype).eval()
    rt.whisper.requires_grad_(False)
    rt.fp = FaceParsing(left_cheek_width=90, right_cheek_width=90)
    print("[musetalk-server] models loaded; ready", flush=True)


def _avatar_cache_complete(avatar_id):
    base = ROOT / "results" / "v15" / "avatars" / avatar_id
    required = ("avator_info.json", "coords.pkl", "latents.pt", "mask_coords.pkl")
    return all((base / name).is_file() for name in required) and (base / "full_imgs").is_dir() and (base / "mask").is_dir()


def _get_avatar(req):
    avatar_id = req["avatar_id"]
    base = ROOT / "results" / "v15" / "avatars" / avatar_id
    with AVATAR_LOCK:
        avatar = AVATARS.get(avatar_id)
        if avatar is not None:
            avatar.batch_size = int(req.get("batch_size", 20))
            print(f"[musetalk-server] avatar {avatar_id}: HIT/memory", flush=True)
            avatar_cache_gc.touch(base)
            return avatar
        complete = _avatar_cache_complete(avatar_id)
        if base.exists() and not complete:
            shutil.rmtree(base)
        print(f"[musetalk-server] avatar {avatar_id}: {'HIT' if complete else 'MISS/build'}", flush=True)
        avatar = rt.Avatar(
            avatar_id=avatar_id,
            video_path=req["video_path"],
            bbox_shift=int(req.get("bbox_shift", 0)),
            batch_size=int(req.get("batch_size", 20)),
            preparation=not complete,
        )
        AVATARS[avatar_id] = avatar
        avatar_cache_gc.touch(base)
        return avatar


def _audio_chunks(req):
    features, librosa_length = rt.audio_processor.get_audio_feature(
        req["audio_path"], weight_dtype=rt.weight_dtype
    )
    return rt.audio_processor.get_whisper_chunk(
        features, rt.device, rt.weight_dtype, rt.whisper, librosa_length,
        fps=int(req.get("fps", 25)),
        audio_padding_length_left=rt.args.audio_padding_length_left,
        audio_padding_length_right=rt.args.audio_padding_length_right,
    )


@torch.inference_mode()
def _infer_group(avatar, reqs):
    started = time.monotonic()
    chunks_by_job = [_audio_chunks(req) for req in reqs]
    combined_chunks, combined_latents = [], []
    latent_cycle = avatar.input_latent_list_cycle
    for chunks in chunks_by_job:
        combined_chunks.extend(chunks)
        combined_latents.extend(latent_cycle[i % len(latent_cycle)] for i in range(len(chunks)))

    requested = sum(int(req.get("batch_size", 20)) for req in reqs)
    gpu_batch = min(MAX_GPU_BATCH, requested)
    print(
        f"[musetalk-server] GPU group={len(reqs)} frames="
        f"{[len(x) for x in chunks_by_job]} batch={gpu_batch}", flush=True,
    )
    frames = []
    gen = datagen(combined_chunks, combined_latents, gpu_batch)
    for whisper_batch, latent_batch in gen:
        audio_features = rt.pe(whisper_batch.to(rt.device))
        latent_batch = latent_batch.to(device=rt.device, dtype=rt.unet.model.dtype)
        predicted = rt.unet.model(
            latent_batch, rt.timesteps, encoder_hidden_states=audio_features
        ).sample
        predicted = predicted.to(device=rt.device, dtype=rt.vae.vae.dtype)
        frames.extend(rt.vae.decode_latents(predicted))

    split, offset = [], 0
    for chunks in chunks_by_job:
        split.append(frames[offset:offset + len(chunks)])
        offset += len(chunks)
    print(
        f"[musetalk-server] GPU group inference done in {time.monotonic() - started:.2f}s",
        flush=True,
    )
    return split


def _encode_job(avatar, req, frames, run_dir):
    if not frames:
        raise RuntimeError("MuseTalk produced no frames")
    fps = int(req.get("fps", 25))
    output_path = Path(req["output_path"]).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    height, width = avatar.frame_list_cycle[0].shape[:2]
    blend_workers = max(1, int(os.environ.get("MUSETALK_ENCODE_WORKERS", "4")))

    def blend_one(item):
        idx, result = item
        cycle_idx = idx % len(avatar.frame_list_cycle)
        bbox = avatar.coord_list_cycle[cycle_idx]
        x1, y1, x2, y2 = bbox
        resized = cv2.resize(result.astype("uint8"), (x2 - x1, y2 - y1))
        return get_image_blending(
            avatar.frame_list_cycle[cycle_idx], resized, bbox,
            avatar.mask_list_cycle[cycle_idx],
            avatar.mask_coords_list_cycle[cycle_idx],
        )

    # One FFmpeg pass: raw blended frames -> H.264 + source audio. Avoid hundreds
    # of PNG writes and the former second remux pass.
    command = [
        "ffmpeg", "-y", "-v", "warning", "-f", "rawvideo",
        "-pix_fmt", "bgr24", "-s:v", f"{width}x{height}", "-r", str(fps),
        "-i", "pipe:0", "-i", req["audio_path"],
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264",
        "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-shortest", str(output_path),
    ]
    started = time.monotonic()
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    try:
        with ThreadPoolExecutor(max_workers=blend_workers) as pool:
            for blended in pool.map(blend_one, enumerate(frames)):
                process.stdin.write(memoryview(blended).cast("B"))
        process.stdin.close()
        returncode = process.wait()
    except Exception:
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        process.kill()
        process.wait()
        raise
    if returncode != 0:
        raise RuntimeError(f"ffmpeg encode failed with exit code {returncode}")
    if not output_path.is_file():
        raise RuntimeError(f"server output missing: {output_path}")
    print(
        f"[musetalk-server] blend+encode {len(frames)} frames in "
        f"{time.monotonic() - started:.2f}s workers={blend_workers}", flush=True,
    )
    return {"ok": True, "output_path": str(output_path)}


def _render_group(reqs):
    avatar = _get_avatar(reqs[0])
    run_dirs = [RUNS_DIR / f"{req['avatar_id']}_{uuid.uuid4().hex}" for req in reqs]
    try:
        with GPU_LOCK:
            frame_groups = _infer_group(avatar, reqs)
        with ThreadPoolExecutor(max_workers=len(reqs)) as pool:
            futures = [
                pool.submit(_encode_job, avatar, req, frames, run_dir)
                for req, frames, run_dir in zip(reqs, frame_groups, run_dirs)
            ]
            return [future.result() for future in futures]
    finally:
        for run_dir in run_dirs:
            shutil.rmtree(run_dir, ignore_errors=True)


def _compatible(left, right):
    keys = ("avatar_id", "bbox_shift", "fps")
    return all(left.get(key, 0) == right.get(key, 0) for key in keys)


def _batch_loop():
    deferred = []
    while True:
        first = deferred.pop(0) if deferred else REQUESTS.get()
        group = [first]
        deadline = time.monotonic() + COALESCE_SECONDS
        while len(group) < MAX_GROUP_SIZE:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                break
            try:
                candidate = REQUESTS.get(timeout=timeout)
            except __import__("queue").Empty:
                break
            if _compatible(first["req"], candidate["req"]):
                group.append(candidate)
            else:
                deferred.append(candidate)

        reqs = [item["req"] for item in group]
        print(
            f"[musetalk-server] dispatch group={len(group)} avatar={reqs[0]['avatar_id']}",
            flush=True,
        )
        try:
            responses = _render_group(reqs)
        except torch.cuda.OutOfMemoryError:
            torch.cuda.empty_cache()
            print("[musetalk-server] grouped batch OOM; safe sequential fallback", flush=True)
            responses = []
            for req in reqs:
                try:
                    responses.extend(_render_group([req]))
                except Exception as exc:
                    traceback.print_exc()
                    responses.append({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        except Exception as exc:
            traceback.print_exc()
            responses = [
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"} for _ in group
            ]
        for item, response in zip(group, responses):
            item["response"] = response
            item["event"].set()


def _handle(conn):
    try:
        line = conn.makefile("r", encoding="utf-8").readline()
        if not line:
            return
        item = {"req": json.loads(line), "event": threading.Event(), "response": None}
        REQUESTS.put(item)
        item["event"].wait()
        response = item["response"]
    except Exception as exc:
        traceback.print_exc()
        response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    conn.sendall((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))


def main():
    os.chdir(ROOT)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    _load_runtime()
    threading.Thread(target=_batch_loop, daemon=True).start()
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(SOCKET_PATH))
    server.listen(16)
    print(f"[musetalk-server] listening {SOCKET_PATH}", flush=True)
    try:
        while True:
            conn, _ = server.accept()
            threading.Thread(target=_handle, args=(conn,), daemon=True).start()
    finally:
        server.close()
        if SOCKET_PATH.exists():
            SOCKET_PATH.unlink()


if __name__ == "__main__":
    main()
