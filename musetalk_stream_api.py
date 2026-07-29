"""HTTP API for one persistent MuseTalk realtime stream.

Run with the MuseTalk virtualenv:
    engines/MuseTalk/.venv/bin/python musetalk_stream_api.py --port 8091
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import threading
import time
import traceback
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
from musetalk.utils.blending import get_image_blending
from musetalk.utils.face_parsing import FaceParsing
from musetalk.utils.utils import datagen, load_all_model
from musetalk_streaming.manager import StreamManager
from musetalk_streaming.models import StreamConfig
from musetalk_streaming.output import RTMPOutput
from musetalk_streaming.session import StreamSession

MANAGER = StreamManager()
GPU_LOCK = threading.Lock()
AVATAR_LOCK = threading.Lock()
AVATARS = {}
MAX_GPU_BATCH = max(1, int(os.getenv("MUSETALK_MAX_GPU_BATCH", "20")))
BLEND_WORKERS = max(1, int(os.getenv("MUSETALK_BLEND_WORKERS", "4")))


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


def get_avatar(avatar_id: str, video_path: str, batch_size: int):
    with AVATAR_LOCK:
        avatar = AVATARS.get(avatar_id)
        if avatar is None:
            cache_dir = MUSETALK_ROOT / "results" / "v15" / "avatars" / avatar_id
            cache_ready = avatar_cache_complete(avatar_id)
            # MuseTalk upstream prompts on stdin when an incomplete cache exists.
            # This API is non-interactive, so discard only that generated partial cache.
            if cache_dir.exists() and not cache_ready:
                shutil.rmtree(cache_dir)
            avatar = rt.Avatar(
                avatar_id=avatar_id, video_path=video_path, bbox_shift=0,
                batch_size=min(MAX_GPU_BATCH, batch_size), preparation=not cache_ready,
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


def audio_chunks(audio_path: str):
    features, length = rt.audio_processor.get_audio_feature(
        audio_path, weight_dtype=rt.weight_dtype
    )
    return rt.audio_processor.get_whisper_chunk(
        features, rt.device, rt.weight_dtype, rt.whisper, length, fps=25,
        audio_padding_length_left=rt.args.audio_padding_length_left,
        audio_padding_length_right=rt.args.audio_padding_length_right,
    )


@torch.inference_mode()
def produce_sentence(avatar, request, emit, cancel) -> None:
    chunks = audio_chunks(request.audio_path)
    request.total_frames = len(chunks)
    waveform, _ = librosa.load(request.audio_path, sr=16_000, mono=True)
    pcm = np.clip(waveform * 32767, -32768, 32767).astype(np.int16)
    latents = avatar.input_latent_list_cycle
    batches = datagen(
        chunks, [latents[i % len(latents)] for i in range(len(chunks))],
        min(MAX_GPU_BATCH, int(avatar.batch_size)),
    )
    frame_index = 0
    total_batch_seconds = 0.0
    total_blend_seconds = 0.0

    def blend_frame(item):
        result, index = item
        cycle = index % len(avatar.frame_list_cycle)
        bbox = avatar.coord_list_cycle[cycle]
        x1, y1, x2, y2 = bbox
        mouth = cv2.resize(result.astype("uint8"), (x2 - x1, y2 - y1))
        return get_image_blending(
            avatar.frame_list_cycle[cycle].copy(), mouth, bbox,
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
                start = frame_index * 640
                emit(frame, pcm[start:start + 640])
                frame_index += 1
            total_blend_seconds += time.monotonic() - blend_started
            del audio_features, latent_batch, predicted, decoded
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
    video_stat = Path(video_path).stat()
    fingerprint = hashlib.sha256(
        f"{video_path}:{video_stat.st_size}:{video_stat.st_mtime_ns}".encode("utf-8")
    ).hexdigest()[:12]
    avatar_id = f"{str(body.get('avatar_id', session_id))}-{fingerprint}"
    config = StreamConfig(
        session_id=session_id, avatar_id=avatar_id, avatar_video=video_path,
        push_url=str(body["push_url"]), fps=int(body.get("fps", 25)),
        warmup_frames=int(body.get("warmup_frames", 25)),
        video_bitrate=str(body.get("video_bitrate", "3500k")),
        audio_bitrate=str(body.get("audio_bitrate", "128k")),
        audio_delay_ms=int(body.get("audio_delay_ms", 300)),
    )
    avatar = get_avatar(avatar_id, video_path, int(body.get("batch_size", 20)))
    output = RTMPOutput(
        config.push_url, config.video_bitrate, config.audio_bitrate,
        config.output_sample_rate, config.audio_delay_ms,
    )
    session = StreamSession(
        config, output, read_idle_frames(video_path),
        lambda request, emit, cancel: produce_sentence(avatar, request, emit, cancel),
        lambda request_id, event, details: print(
            f"[stream] session={session_id} request={request_id} event={event}",
            flush=True,
        ),
    )
    MANAGER.replace(session)
    session.start()
    return session.get_status()


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

    def _route(self) -> tuple[str | None, str | None]:
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if parts[:2] != ["api", "streams"]:
            return None, None
        return (parts[2] if len(parts) > 2 else None,
                parts[3] if len(parts) > 3 else None)

    def do_POST(self) -> None:
        session_id, action = self._route()
        try:
            body = self._body()
            if session_id is None:
                result = start_session(body)
                self._json(201, result)
                return
            session = MANAGER.get(session_id)
            if action == "enqueue":
                audio_path = str(Path(body["audio_path"]).resolve())
                if not Path(audio_path).is_file():
                    raise ValueError("audio_path does not exist")
                session.enqueue(
                    str(body["request_id"]), audio_path, int(body.get("priority", 0)),
                    bool(body.get("interrupt", False)),
                )
            elif action == "interrupt":
                session.interrupt()
            else:
                self._json(404, {"error": "not found"})
                return
            self._json(202, session.get_status())
        except (KeyError, ValueError) as exc:
            self._json(400, {"error": str(exc)})
        except RuntimeError as exc:
            self._json(409, {"error": str(exc)})
        except Exception as exc:
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
