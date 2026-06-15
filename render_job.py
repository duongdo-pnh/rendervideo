"""Standalone render runner — one job per process invocation.

queue_worker.py spawns this as a subprocess so a CUDA OOM / segfault / pipeline crash
kills only this process, never the long-running worker. It mirrors process_video() in
gradio_app.py (downscale -> prepare_carrier -> diffusion -> optional GFPGAN mouth) but
imports NO gradio, so it runs in a clean subprocess.

Exit 0 = success and the output file exists at --output. Any non-zero exit = failure;
the worker reads the captured log and decides retry vs fail.
"""
import argparse
import contextlib
import fcntl
import os
import subprocess
import sys
from pathlib import Path

import cv2
import torch

# Cross-process GPU lock: both "Render ngay" (in web_ui) and the worker subprocess run
# render() here, so an flock on this one file serializes ALL diffusion across processes —
# they can never collide on the GPU (OOM) or on the shared temp dir.
GPU_LOCK_PATH = Path(__file__).parent / ".gpu.lock"


@contextlib.contextmanager
def gpu_lock():
    with open(GPU_LOCK_PATH, "w") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("[render_job] GPU đang bận (render khác đang chạy) — chờ tới lượt...", flush=True)
            fcntl.flock(lf, fcntl.LOCK_EX)  # block until the other render releases
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)

# Autotune cuDNN for the fixed diffusion chunk shape (matches gradio_app.py).
torch.backends.cudnn.benchmark = True

from omegaconf import OmegaConf

from scripts.inference import main as inference_main
from extend_video import prepare_carrier
from restore_mouth_gfpgan import restore_mouth

# Output resolution presets (target SHORTER side). None = keep source.
OUT_RES = {"Gốc": None, "1080": 1080, "720": 720}


def _maybe_downscale(video_path, target_short, work_dir):
    """Downscale input to target shorter-side (output res == input res in LatentSync).
    No-op if already <= target. Copied from gradio_app.py:_maybe_downscale."""
    if not target_short:
        return video_path
    cap = cv2.VideoCapture(video_path)
    w, h = int(cap.get(3)), int(cap.get(4))
    cap.release()
    if min(w, h) <= target_short:
        return video_path
    out = os.path.join(work_dir, f"_in_{target_short}p.mp4")
    vf = f"scale='if(gt(iw,ih),-2,{target_short})':'if(gt(iw,ih),{target_short},-2)'"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", video_path, "-vf", vf,
         "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-an", out],
        check=True,
    )
    print(f"[render_job] downscale {w}x{h} -> shorter side {target_short}", flush=True)
    return out


def _build_args(video_path, audio_path, output_path, checkpoint, steps, guidance, seed, temp_dir):
    parser = argparse.ArgumentParser()
    parser.add_argument("--inference_ckpt_path", type=str, required=True)
    parser.add_argument("--video_path", type=str, required=True)
    parser.add_argument("--audio_path", type=str, required=True)
    parser.add_argument("--video_out_path", type=str, required=True)
    parser.add_argument("--inference_steps", type=int, default=20)
    parser.add_argument("--guidance_scale", type=float, default=1.5)
    parser.add_argument("--temp_dir", type=str, default="temp")
    parser.add_argument("--seed", type=int, default=1247)
    parser.add_argument("--enable_deepcache", action="store_true")
    return parser.parse_args([
        "--inference_ckpt_path", checkpoint,
        "--video_path", video_path,
        "--audio_path", audio_path,
        "--video_out_path", output_path,
        "--inference_steps", str(steps),
        "--guidance_scale", str(guidance),
        "--seed", str(seed),
        "--temp_dir", temp_dir,
        "--enable_deepcache",
    ])


def render(video_path, audio_path, output_path, config_path, checkpoint,
           guidance, steps, seed, enhance_mouth, enhance_region, out_res):
    work_dir = Path(output_path).parent
    work_dir.mkdir(parents=True, exist_ok=True)

    video_path = Path(video_path).absolute().as_posix()
    audio_path = Path(audio_path).absolute().as_posix()
    output_path = Path(output_path).absolute().as_posix()

    # Output resolution: downscale input to chosen shorter side (output res = input res).
    video_path = _maybe_downscale(video_path, OUT_RES.get(out_res), str(work_dir))

    # If the carrier video is shorter than the audio, extend it (forward loop + crossfade);
    # returns seam indices so the mouth temporal smooth won't ghost across discontinuities.
    ext_path = str(work_dir / "_carrier_ext.mp4")
    try:
        video_path, seams = prepare_carrier(video_path, audio_path, ext_path)
    except Exception as e:
        print(f"[render_job] prepare_carrier failed ({e}); using original video", flush=True)
        seams = []

    print(f"[render_job] config={config_path} ckpt={checkpoint} gfpgan={enhance_mouth}", flush=True)
    config = OmegaConf.load(config_path)
    config["run"].update({"guidance_scale": guidance, "inference_steps": steps})

    # Per-render temp dir (the pipeline WIPES temp_dir at start) — unique so a concurrent
    # render in another process can't clobber our temp/synced.mp4. Stem carries a timestamp
    # for render-now and is per-job for the worker, so it's unique either way.
    raw_out = str(work_dir / "_diffusion_raw.mp4")
    temp_dir = str(work_dir / f"temp_{Path(output_path).stem}")
    args = _build_args(video_path, audio_path, raw_out, checkpoint, steps, guidance, seed, temp_dir)

    # Serialize the GPU-heavy section across processes (diffusion + GFPGAN).
    with gpu_lock():
        inference_main(config=config, args=args)
        print("[render_job] diffusion done", flush=True)

        if enhance_mouth:
            try:
                torch.cuda.empty_cache()
            except Exception:
                pass
            print(f"[render_job] GFPGAN enhance (region={enhance_region}, {len(seams)} seam(s))", flush=True)
            restore_mouth(raw_out, output_path, region=enhance_region, seams=seams,
                          gfpgan_alpha=0.7, sharpen=0.0)
        else:
            # Fast mode: raw diffusion output, no GFPGAN.
            os.replace(raw_out, output_path)

    if not os.path.exists(output_path):
        raise RuntimeError(f"render finished but output missing: {output_path}")
    print(f"[render_job] OK -> {output_path}", flush=True)


def main():
    ap = argparse.ArgumentParser(description="LatentSync single-job render runner")
    ap.add_argument("--video", required=True)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--checkpoint", required=True)
    ap.add_argument("--guidance", type=float, default=1.5)
    ap.add_argument("--steps", type=int, default=20)
    ap.add_argument("--seed", type=int, default=1247)
    ap.add_argument("--enhance_mouth", type=int, default=1, help="1=GFPGAN on (default), 0=raw diffusion")
    ap.add_argument("--enhance_region", default="mouth", choices=["mouth", "face"])
    ap.add_argument("--out_res", default="720", choices=list(OUT_RES.keys()))
    a = ap.parse_args()
    try:
        render(a.video, a.audio, a.output, a.config, a.checkpoint, a.guidance, a.steps,
               a.seed, bool(a.enhance_mouth), a.enhance_region, a.out_res)
    except Exception as e:
        print(f"[render_job] ERROR: {e}", file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
