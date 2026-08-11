"""HTTP API for one persistent MuseTalk realtime stream.

Run with the MuseTalk virtualenv:
    engines/MuseTalk/.venv/bin/python musetalk_stream_api.py --port 8091
"""
from __future__ import annotations

import argparse
import cgi
import gc
import hashlib
import json
import os
import shutil
import sys
import threading
import time
import traceback
import uuid
import wave
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

# Conservative defaults keep the web UI and encoder responsive while MuseTalk runs.
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")

APP_ROOT = Path(__file__).resolve().parent
MUSETALK_ROOT = APP_ROOT / "engines" / "MuseTalk"
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(MUSETALK_ROOT))
os.chdir(MUSETALK_ROOT)

import cv2
import librosa
import numpy as np
import torch
from transformers import WhisperModel

import scripts.realtime_inference as rt
from musetalk.utils.audio_processor import AudioProcessor
from musetalk.utils.face_parsing import FaceParsing
from musetalk.utils.utils import datagen, load_all_model
from musetalk_streaming.manager import StreamManager
from musetalk_streaming.models import StreamConfig
from musetalk_streaming.output import RTMPOutput, ReLiveClipOutput
from musetalk_streaming.session import StreamSession

MANAGER = StreamManager()
GPU_LOCK = threading.Lock()
AVATAR_LOCK = threading.Lock()
AVATARS = {}
# Hashing a clip costs a disk read, and _ensure_session() on the caller side
# retries the same file often, so remember the digest per (path, size, mtime).
_FINGERPRINTS = {}
# Avatar prep (frame extraction, landmark, mask/latent) runs ~40 min for a
# 3 min clip. start_session() used to run inline in the POST handler, so the
# HTTP client held one connection open the whole time — Gradio's own request
# timeout (well under 40 min) fired first and showed a scary error even though
# the build kept running fine in the background. Track it here instead so
# do_POST can return immediately and callers poll do_GET for real progress.
PREP_LOCK = threading.Lock()
PREPARING: dict[str, dict] = {}
# TRẦN batch, không phải mục tiêu. Batch to = kernel CUDA chạy dài = OBS phải xếp
# hàng sau nó để composite -> rớt frame ĐÚNG LÚC avatar đang nói. Đo trên 5090
# (2026-08-04, avatar đọc + đang encode, 60s mỗi mức):
#   batch 20 -> OBS 27,1 fps, skip 11,3%     batch 8 -> 30,0 fps, skip 0,19%
#   batch  6 -> OBS 30,0 fps, skip 0,00%     batch 2 -> MuseTalk đói buffer
# Giữ trần thấp để dù caller cũ truyền 20 vẫn không phá được độ mượt.
MAX_GPU_BATCH = max(1, int(os.getenv("MUSETALK_MAX_GPU_BATCH", "8")))
BLEND_WORKERS = max(1, int(os.getenv("MUSETALK_BLEND_WORKERS", "16")))
AUDIO_UPLOAD_DIR = APP_ROOT / "uploads" / "stream_audio"
MAX_AUDIO_UPLOAD_BYTES = int(os.getenv("MUSETALK_MAX_AUDIO_UPLOAD_MB", "100")) * 1024 * 1024
ALLOWED_AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
MAX_AUDIO_CHUNK_SECONDS = max(
    5.0, float(os.getenv("MUSETALK_MAX_AUDIO_CHUNK_SECONDS", "5"))
)
AUDIO_CHUNK_DIR = APP_ROOT / "uploads" / "stream_audio_chunks"


def load_runtime() -> None:
    cv2.setNumThreads(max(1, int(os.getenv("MUSETALK_OPENCV_THREADS", "2"))))
    torch.set_num_threads(max(1, int(os.getenv("MUSETALK_TORCH_THREADS", "2"))))
    torch.backends.cudnn.benchmark = True
    torch.backends.cuda.matmul.allow_tf32 = True
    torch.backends.cudnn.allow_tf32 = True
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


def avatar_cache_complete(avatar_id: str) -> bool:
    base = MUSETALK_ROOT / "results" / "v15" / "avatars" / avatar_id
    required = ("avator_info.json", "coords.pkl", "latents.pt", "mask_coords.pkl")
    return (
        all((base / name).is_file() for name in required)
        and (base / "full_imgs").is_dir() and (base / "mask").is_dir()
    )


def video_fingerprint(video_path: str) -> str:
    """Hash the clip's bytes, same scheme as musetalk_adapter._video_cache_key.

    Preparing one avatar costs ~40 min and ~13 GB, so the cache key must depend
    on the content alone.  Keying it on path+mtime — as this did — meant every
    rewrite of an identical clip (the UI re-normalises the Facebook avatar on
    each Start stream) produced a fresh key and re-ran the whole preparation.
    """
    stat = Path(video_path).stat()
    memo_key = (video_path, stat.st_size, stat.st_mtime_ns)
    cached = _FINGERPRINTS.get(memo_key)
    if cached is not None:
        return cached
    digest = hashlib.sha256()
    with open(video_path, "rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    fingerprint = digest.hexdigest()[:16]
    _FINGERPRINTS[memo_key] = fingerprint
    return fingerprint


def _avatar_max_seconds(body: dict) -> float | None:
    """How much of avatar_video to extract/cache, from body["avatar_max_seconds"].

    None/0/absent means the full clip, unchanged. Preparing a long sample
    (frame extraction + landmark + mask/latent) scales linearly with its
    length even though the realtime idle loop only ever cycles
    frame_list_cycle forward+backward — trimming a long source to a short
    loop (e.g. 8-15s) cuts prep time, disk and RAM proportionally.
    """
    value = body.get("avatar_max_seconds")
    if value in (None, ""):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.5, seconds) if seconds > 0 else None


def _avatar_id_for(video_path: str, max_seconds: float | None) -> str:
    avatar_id = f"avatar-{video_fingerprint(video_path)}"
    if max_seconds:
        # Distinct trims of the same source are distinct caches — otherwise
        # a 10s trim and the full clip would collide on the same avatar_id
        # and silently reuse whichever was built first.
        avatar_id += f"-{int(round(max_seconds))}s"
    return avatar_id


def get_avatar(avatar_id: str, video_path: str, batch_size: int, max_seconds: float | None = None):
    with AVATAR_LOCK:
        avatar = AVATARS.get(avatar_id)
        if avatar is None:
            # Each resident avatar holds its whole frame/mask cycle in RAM —
            # ~19-27 GB per clip on this box, enough on its own to near the
            # 31 GB ceiling. AVATARS used to keep every avatar it ever built,
            # and a stopped-but-not-replaced StreamSession's closure keeps its
            # avatar alive regardless — building a second, different avatar
            # while the first stayed resident OOM-killed this process (kernel
            # log, 2026-08-07 17:16, anon-rss 27 GB). Only one stream is ever
            # active (StreamManager's own MVP design) — matching that here
            # means dropping whatever's resident before building a new one.
            if AVATARS:
                MANAGER.stop_all()
                AVATARS.clear()
                gc.collect()
            cache_dir = MUSETALK_ROOT / "results" / "v15" / "avatars" / avatar_id
            cache_ready = avatar_cache_complete(avatar_id)
            # MuseTalk upstream prompts on stdin when an incomplete cache exists.
            # This API is non-interactive, so discard only that generated partial cache.
            if cache_dir.exists() and not cache_ready:
                shutil.rmtree(cache_dir)
            avatar = rt.Avatar(
                avatar_id=avatar_id, video_path=video_path, bbox_shift=0,
                batch_size=min(MAX_GPU_BATCH, batch_size), preparation=not cache_ready,
                max_seconds=max_seconds,
            )
            AVATARS[avatar_id] = avatar
        else:
            avatar.batch_size = min(MAX_GPU_BATCH, batch_size)
        return avatar


def read_idle_frames(video_path: str, limit: int = 250) -> list[np.ndarray]:
    capture = cv2.VideoCapture(video_path)
    frames = []
    try:
        while len(frames) < limit:
            ok, frame = capture.read()
            if not ok:
                break
            frames.append(frame)
    finally:
        capture.release()
    if not frames:
        raise ValueError("avatar_video has no readable frames")
    return frames


def audio_chunks(audio_path: str, fps: float):
    features, length = rt.audio_processor.get_audio_feature(
        audio_path, weight_dtype=rt.weight_dtype
    )
    return rt.audio_processor.get_whisper_chunk(
        features, rt.device, rt.weight_dtype, rt.whisper, length, fps=fps,
        audio_padding_length_left=rt.args.audio_padding_length_left,
        audio_padding_length_right=rt.args.audio_padding_length_right,
    )


def split_audio_for_stream(audio_path: str, request_id: str, delete_source: bool = False):
    """Keep one logical voice timeline; inference already streams it in GPU batches."""
    path = Path(audio_path)
    return [(request_id, str(path), delete_source)]


def blend_cached_frame(
    image: np.ndarray, face: np.ndarray, face_box, mask_array: np.ndarray, crop_box
) -> np.ndarray:
    """Pixel-identical cached-mask blend without PIL full-frame conversions."""
    x, y, x1, y1 = face_box
    x_s, y_s, x_e, y_e = crop_box
    output = image.copy()
    background = output[y_s:y_e, x_s:x_e]
    foreground = background.copy()
    foreground[y - y_s:y1 - y_s, x - x_s:x1 - x_s] = face
    if mask_array.ndim == 3:
        mask_array = mask_array[..., 0]
    alpha = mask_array.astype(np.uint16, copy=False)[..., None]
    background[:] = (
        foreground.astype(np.uint16) * alpha
        + background.astype(np.uint16) * (255 - alpha)
        + 127
    ) // 255
    return output


@torch.inference_mode()
def produce_sentence(
    avatar, request, emit, cancel, fps: int, frame_repeat: int = 1,
    frame_cursor: list[int] | None = None,
) -> None:
    frame_repeat = max(1, int(frame_repeat))
    inference_fps = fps / frame_repeat
    all_chunks = audio_chunks(request.audio_path, inference_fps)
    full_total_frames = len(all_chunks) * frame_repeat
    start_frame = min(max(0, int(request.start_frame)), full_total_frames)
    start_chunk = min(len(all_chunks), start_frame // frame_repeat)
    chunks = all_chunks[start_chunk:]
    request.total_frames = max(0, full_total_frames - start_frame)
    waveform, _ = librosa.load(request.audio_path, sr=16_000, mono=True)
    pcm_start = round(start_frame * 16_000 / fps)
    waveform = waveform[pcm_start:]
    pcm = np.clip(waveform * 32767, -32768, 32767).astype(np.int16)
    latents = avatar.input_latent_list_cycle
    freeze_frame_index = request.freeze_frame_index
    base_frame_index = (
        freeze_frame_index
        if freeze_frame_index is not None
        else (
            request.start_driver_frame_index
            if request.start_driver_frame_index is not None
            else (frame_cursor[0] if frame_cursor is not None else 0)
        )
    )
    batches = datagen(
        chunks,
        [
            latents[
                (
                    base_frame_index
                    if freeze_frame_index is not None
                    else base_frame_index + i
                ) % len(latents)
            ]
            for i in range(len(chunks))
        ],
        min(MAX_GPU_BATCH, int(avatar.batch_size)),
    )
    frame_index = 0
    total_batch_seconds = 0.0
    total_blend_seconds = 0.0

    def blend_frame(item):
        result, index = item
        cycle = (
            base_frame_index
            if freeze_frame_index is not None
            else base_frame_index + index
        ) % len(avatar.frame_list_cycle)
        bbox = avatar.coord_list_cycle[cycle]
        x1, y1, x2, y2 = bbox
        mouth = cv2.resize(result.astype("uint8"), (x2 - x1, y2 - y1))
        return blend_cached_frame(
            avatar.frame_list_cycle[cycle], mouth, bbox,
            avatar.mask_list_cycle[cycle], avatar.mask_coords_list_cycle[cycle],
        )

    with ThreadPoolExecutor(max_workers=BLEND_WORKERS) as blend_pool:
        for whisper_batch, latent_batch in batches:
            if cancel.is_set():
                break
            batch_started = time.monotonic()
            with GPU_LOCK:
                audio_features = rt.pe(whisper_batch.to(rt.device, non_blocking=True))
                latent_batch = latent_batch.to(
                    device=rt.device, dtype=rt.unet.model.dtype, non_blocking=True
                )
                predicted = rt.unet.model(
                    latent_batch, rt.timesteps, encoder_hidden_states=audio_features
                ).sample.to(device=rt.device, dtype=rt.vae.vae.dtype)
                decoded = rt.vae.decode_latents(predicted)
            total_batch_seconds += time.monotonic() - batch_started

            usable = min(len(decoded), len(chunks) - frame_index)
            blend_started = time.monotonic()
            work = [(decoded[offset], frame_index + offset) for offset in range(usable)]
            frames = blend_pool.map(blend_frame, work)
            for frame in frames:
                if cancel.is_set():
                    break
                # At 720p the 3090 sustains about 12.5 generated mouth frames
                # per second. Duplicate each generated frame into the output
                # FPS clock when a lower render FPS is used.
                # output clock while advancing audio on every output frame.
                for repeat_index in range(frame_repeat):
                    output_index = frame_index * frame_repeat + repeat_index
                    start = round(output_index * 16_000 / fps)
                    end = round((output_index + 1) * 16_000 / fps)
                    driver_index = (
                        base_frame_index
                        if freeze_frame_index is not None
                        else base_frame_index + frame_index
                    )
                    emit(frame, pcm[start:end], driver_index)
                frame_index += 1
            total_blend_seconds += time.monotonic() - blend_started
            del audio_features, latent_batch, predicted, decoded
    if frame_cursor is not None and freeze_frame_index is None:
        # Preserve the driver-video phase across all five-second voice chunks.
        # Only the mouth changes; the body/background never jump back to frame 0.
        frame_cursor[0] = base_frame_index + frame_index
    return {
        "frames": frame_index,
        "gpu_batch_ms": total_batch_seconds * 1000,
        "blend_ms": total_blend_seconds * 1000,
        "gpu_memory_bytes": torch.cuda.memory_allocated() if torch.cuda.is_available() else 0,
    }


def start_session(body: dict) -> dict:
    video_path = str(Path(body["avatar_video"]).resolve())
    if not Path(video_path).is_file():
        raise ValueError("avatar_video does not exist")
    session_id = str(body["session_id"])
    # Keyed on the clip alone — NOT on the caller's avatar_id.  The UI posts
    # avatar_id="facebook-live" while AI-live posts "facebook-live-realtime-720"
    # for the very same file, which used to build (and keep) two 13 GB caches.
    max_seconds = _avatar_max_seconds(body)
    avatar_id = _avatar_id_for(video_path, max_seconds)
    # Same rule as the normal MuseTalk renderer: output keeps the driver's
    # native frame size/aspect ratio.  Realtime used to default to 1280x720,
    # which stretched portrait and square avatars.
    idle_frames = read_idle_frames(video_path)
    source_height, source_width = idle_frames[0].shape[:2]
    width = max(2, source_width // 2 * 2)
    height = max(2, source_height // 2 * 2)
    output_mode = str(body.get("output_mode", "rtmp")).lower()
    push_url = str(body.get("push_url", ""))
    # The desktop Facebook session is a persistent realtime feed consumed by
    # OBS. Do not let an empty UI request replace it with legacy MP4 output.
    if session_id == "facebook-live" and output_mode == "relive":
        output_mode = "udp"
        push_url = "udp://127.0.0.1:5000?pkt_size=1316"
    config = StreamConfig(
        session_id=session_id, avatar_id=avatar_id, avatar_video=video_path,
        push_url=push_url, fps=int(body.get("fps", 25)),
        output_fps=int(body["output_fps"]) if body.get("output_fps") else None,
        width=width, height=height,
        warmup_frames=int(body.get("warmup_frames", 30)),
        video_queue_frames=int(body.get("video_queue_frames", 150)),
        interrupt_cover_frames=int(body.get("interrupt_cover_frames", 20)),
        video_bitrate=str(body.get("video_bitrate", "3500k")),
        audio_bitrate=str(body.get("audio_bitrate", "128k")),
        audio_delay_ms=int(body.get("audio_delay_ms", 300)),
    )
    avatar = get_avatar(avatar_id, video_path, int(body.get("batch_size", 6)), max_seconds)
    if output_mode == "relive":
        output = ReLiveClipOutput(
            APP_ROOT / "outputs" / "relive_clips",
            str(body.get("relive_callback_url", "http://127.0.0.1:7864/api/musetalk/clip")),
        )
    elif output_mode in {"rtmp", "udp"}:
        output = RTMPOutput(config.push_url, config.video_bitrate, config.audio_bitrate,
                            config.output_sample_rate, config.audio_delay_ms,
                            output_fps=config.output_fps)
    else:
        raise ValueError("output_mode must be rtmp, udp or relive")
    frame_cursor = [0]
    session = StreamSession(
        config, output, idle_frames,
        lambda request, emit, cancel: produce_sentence(
            avatar, request, emit, cancel, config.fps,
            frame_repeat=2 if session_id == "facebook-live" and config.fps == 25 else 1,
            frame_cursor=frame_cursor,
        ),
        lambda request_id, event, details: print(
            f"[stream] session={session_id} request={request_id} event={event} details={details}",
            flush=True,
        ),
    )
    MANAGER.replace(session)
    session.start()
    return session.get_status()


def _prep_frame_total(video_path: str, max_seconds: float | None = None) -> int | None:
    capture = cv2.VideoCapture(video_path)
    try:
        total = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        if max_seconds:
            fps = capture.get(cv2.CAP_PROP_FPS) or 0
            if fps > 0:
                total = min(total, int(round(max_seconds * fps)))
    finally:
        capture.release()
    return total if total > 0 else None


def prep_status(session_id: str) -> dict | None:
    """Progress for a session whose avatar cache is still being prepared.

    rt.Avatar's preparation writes its own tqdm bars to stdout with no
    programmatic hook, but it does write frames/masks to disk as it goes —
    counting those files is the same trick used to watch this build by hand,
    just automated so the UI can poll it instead.
    """
    with PREP_LOCK:
        entry = PREPARING.get(session_id)
        if entry is None:
            return None
        avatar_id = entry["avatar_id"]
        cache_ready = entry["cache_ready"]
        frame_total = entry.get("frame_total")
        started_at = entry["started_at"]
        error = entry.get("error")
    if error:
        return {"session_id": session_id, "status": "error", "avatar_id": avatar_id, "error": error}
    base = MUSETALK_ROOT / "results" / "v15" / "avatars" / avatar_id
    n_full = len(os.listdir(base / "full_imgs")) if (base / "full_imgs").is_dir() else 0
    n_mask = len(os.listdir(base / "mask")) if (base / "mask").is_dir() else 0
    stage, percent, detail = "starting", 0.0, ""
    if frame_total:
        if n_mask > 0:
            # mask/ ends up with frame_total*2 files (forward + reversed
            # idle-loop halves), but only the first frame_total are actual
            # GPU/CPU work -- the reversed half is written from already-
            # computed results (Avatar.prepare_material dedup), so it lands
            # on disk in a fast near-instant burst at the very end. Track
            # percent against the compute-bound half so it doesn't stall at
            # 50% for most of the stage then jump straight to 100%.
            n_computed = min(n_mask, frame_total)
            stage, detail = "mask_latent", f"{n_computed}/{frame_total}"
            percent = min(99.0, n_computed * 100.0 / frame_total)
        elif n_full >= frame_total:
            # Landmark detection writes no per-frame files, but it does write
            # a small .landmark_progress marker (just the frame count as
            # text) every 20 frames -- read that instead of reporting a flat
            # 0% for the whole stage, which used to look identical to a hang.
            n_landmark = 0
            landmark_progress_path = base / ".landmark_progress"
            if landmark_progress_path.is_file():
                try:
                    n_landmark = int(landmark_progress_path.read_text().strip())
                except (ValueError, OSError):
                    n_landmark = 0
            stage, detail = "landmark", f"{n_landmark}/{frame_total}"
            percent = min(99.0, n_landmark * 100.0 / frame_total)
        elif n_full > 0:
            stage, detail = "frames", f"{n_full}/{frame_total}"
            percent = n_full * 100.0 / frame_total
    return {
        "session_id": session_id, "status": "preparing", "avatar_id": avatar_id,
        "cache_ready": cache_ready, "elapsed_seconds": round(time.monotonic() - started_at, 1),
        "prep_stage": stage, "prep_percent": round(percent, 1), "prep_detail": detail,
    }


def begin_session(body: dict) -> dict:
    """Non-blocking counterpart to start_session(): kick off prep in a
    background thread and return immediately so the HTTP request never
    outlives the ~40 min build. Callers poll GET /api/streams/<id> for
    progress until it stops reporting status == "preparing"."""
    video_path = str(Path(body["avatar_video"]).resolve())
    if not Path(video_path).is_file():
        raise ValueError("avatar_video does not exist")
    session_id = str(body["session_id"])
    max_seconds = _avatar_max_seconds(body)
    avatar_id = _avatar_id_for(video_path, max_seconds)
    cache_ready = avatar_cache_complete(avatar_id)
    # prep_status() takes PREP_LOCK itself, so it must only ever be called
    # AFTER this block releases it — threading.Lock isn't reentrant, calling
    # it from inside the `with` below deadlocks this thread on itself, and
    # since PREP_LOCK is process-global that wedges every other session too.
    start_worker = False
    with PREP_LOCK:
        existing = PREPARING.get(session_id)
        if existing is not None and existing.get("thread") is not None and existing["thread"].is_alive():
            entry = existing
        else:
            entry = {
                "avatar_id": avatar_id, "started_at": time.monotonic(), "cache_ready": cache_ready,
                "error": None, "thread": None,
                "frame_total": None if cache_ready else _prep_frame_total(video_path, max_seconds),
            }
            PREPARING[session_id] = entry
            start_worker = True

    if start_worker:
        def worker() -> None:
            try:
                start_session(body)
            except Exception as exc:  # noqa: BLE001 — surfaced to the poller, not swallowed
                with PREP_LOCK:
                    entry["error"] = f"{type(exc).__name__}: {exc}"
                traceback.print_exc()
            else:
                with PREP_LOCK:
                    PREPARING.pop(session_id, None)

        thread = threading.Thread(target=worker, daemon=True)
        with PREP_LOCK:
            entry["thread"] = thread
        thread.start()
    return prep_status(session_id)


class Handler(BaseHTTPRequestHandler):
    server_version = "MuseTalkStream/1.0"

    def _json(self, status: int, payload: dict) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _jpeg(self, frame: np.ndarray) -> None:
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            raise RuntimeError("cannot encode preview frame")
        payload = encoded.tobytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def _multipart_audio(self) -> dict:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            raise ValueError("Content-Type must be multipart/form-data")
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            raise ValueError("empty multipart request")
        if length > MAX_AUDIO_UPLOAD_BYTES:
            raise ValueError(
                f"audio upload exceeds {MAX_AUDIO_UPLOAD_BYTES // (1024 * 1024)} MB"
            )
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": content_type,
                "CONTENT_LENGTH": str(length),
            },
            keep_blank_values=True,
        )
        if "audio" not in form:
            raise ValueError("multipart field audio is required")
        item = form["audio"]
        if isinstance(item, list):
            item = item[0]
        if not getattr(item, "filename", None) or item.file is None:
            raise ValueError("audio must be a file")
        suffix = Path(item.filename).suffix.lower()
        if suffix not in ALLOWED_AUDIO_SUFFIXES:
            raise ValueError(
                "unsupported audio format; use wav, mp3, m4a, aac, flac, ogg or opus"
            )
        AUDIO_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        destination = AUDIO_UPLOAD_DIR / f"{uuid.uuid4().hex}{suffix}"
        try:
            with open(destination, "wb") as output:
                shutil.copyfileobj(item.file, output, length=1024 * 1024)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return {
            "audio_path": str(destination),
            "request_id": form.getfirst("request_id") or f"postman-{int(time.time())}",
            "priority": int(form.getfirst("priority") or 0),
            "interrupt": str(form.getfirst("interrupt") or "false").lower()
            in {"1", "true", "yes", "on"},
        }

    def _route(self) -> tuple[str | None, str | None]:
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if parts[:2] != ["api", "streams"]:
            return None, None
        return (parts[2] if len(parts) > 2 else None,
                parts[3] if len(parts) > 3 else None)

    def do_POST(self) -> None:
        session_id, action = self._route()
        uploaded_path = None
        enqueued_chunks = 0
        try:
            if session_id is None:
                result = begin_session(self._body())
                self._json(202, result)
                return
            session = MANAGER.get(session_id)
            if action == "enqueue-file":
                body = self._multipart_audio()
                uploaded_path = body["audio_path"]
                chunks = split_audio_for_stream(
                    uploaded_path, str(body["request_id"]), delete_source=True
                )
                session.enqueue_batch(
                    [(chunk_id, chunk_path, int(body.get("priority", 0)), delete_after_use)
                     for chunk_id, chunk_path, delete_after_use in chunks],
                    interrupt=bool(body.get("interrupt", False)),
                )
                enqueued_chunks = len(chunks)
                uploaded_path = None  # split helper/session owns the upload now
            elif action == "enqueue":
                body = self._body()
                audio_path = str(Path(body["audio_path"]).resolve())
                if not Path(audio_path).is_file():
                    raise ValueError("audio_path does not exist")
                chunks = split_audio_for_stream(audio_path, str(body["request_id"]))
                session.enqueue_batch(
                    [(chunk_id, chunk_path, int(body.get("priority", 0)), delete_after_use)
                     for chunk_id, chunk_path, delete_after_use in chunks],
                    interrupt=bool(body.get("interrupt", False)),
                )
                enqueued_chunks = len(chunks)
            elif action == "interrupt":
                self._body()
                session.interrupt()
            else:
                self._json(404, {"error": "not found"})
                return
            status = session.get_status()
            if enqueued_chunks:
                status["audio_chunks_enqueued"] = enqueued_chunks
            self._json(202, status)
        except (KeyError, ValueError) as exc:
            if uploaded_path:
                Path(uploaded_path).unlink(missing_ok=True)
            self._json(400, {"error": str(exc)})
        except RuntimeError as exc:
            if uploaded_path:
                Path(uploaded_path).unlink(missing_ok=True)
            self._json(409, {"error": str(exc)})
        except Exception as exc:
            if uploaded_path:
                Path(uploaded_path).unlink(missing_ok=True)
            traceback.print_exc()
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_GET(self) -> None:
        session_id, action = self._route()
        if session_id is None:
            self._json(404, {"error": "not found"})
            return
        try:
            session = MANAGER.get(session_id)
            if action == "preview.jpg":
                self._jpeg(session.get_preview_frame())
            elif action is None:
                self._json(200, session.get_status())
            else:
                self._json(404, {"error": "not found"})
        except KeyError as exc:
            if action == "preview.jpg":
                placeholder = np.zeros((720, 1280, 3), dtype=np.uint8)
                cv2.putText(
                    placeholder, "Chua Start stream", (390, 350),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.5, (255, 255, 255), 3, cv2.LINE_AA,
                )
                self._jpeg(placeholder)
            elif action is None:
                prep = prep_status(session_id)
                if prep is not None:
                    self._json(200, prep)
                else:
                    self._json(404, {"error": str(exc)})
            else:
                self._json(404, {"error": str(exc)})

    def do_DELETE(self) -> None:
        session_id, action = self._route()
        if session_id is None or action is not None:
            self._json(404, {"error": "not found"})
            return
        try:
            MANAGER.stop(session_id)
            self._json(200, MANAGER.get(session_id).get_status())
        except KeyError as exc:
            self._json(404, {"error": str(exc)})

    def log_message(self, fmt: str, *args) -> None:
        print(f"[stream-api] {self.address_string()} {fmt % args}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    args = parser.parse_args()
    load_runtime()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[stream-api] listening http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    finally:
        for status in MANAGER.statuses():
            MANAGER.stop(status["session_id"])
        server.server_close()


if __name__ == "__main__":
    main()
