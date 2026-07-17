"""Standalone render runner — one job per process invocation.

queue_worker.py spawns this as a subprocess so a CUDA OOM / segfault / pipeline crash
kills only this process, never the long-running worker. It mirrors process_video() in
gradio_app.py (downscale -> prepare_carrier -> diffusion -> optional GFPGAN mouth) but
imports NO gradio, so it runs in a clean subprocess.

Exit 0 = success and the output file exists at --output. Any non-zero exit = failure;
the worker reads the captured log and decides retry vs fail.
"""
import os
import sys
# Chạy 'python ...' trực tiếp (không activate conda env) -> PATH thiếu bin của env nên không thấy
# ffmpeg/ffprobe. Thêm thư mục bin của python hiện hành vào PATH để mọi subprocess gọi ffmpeg chạy được.
_envbin = os.path.dirname(sys.executable)
if _envbin and _envbin not in os.environ.get("PATH", "").split(os.pathsep):
    os.environ["PATH"] = _envbin + os.pathsep + os.environ.get("PATH", "")

# LƯU Ý (đã kiểm chứng 2026-06-15): KHÔNG ép insightface/onnxruntime chạy CUDAExecutionProvider.
# Diffusion nhanh hơn ~43% (5.55->3.22 s/it) NHƯNG output bị HỎNG — vẽ ô ĐEN lên mặt (mediapipe
# dò mặt 0/98 frame). insightface PHẢI chạy CPUExecutionProvider để kết quả dò mặt đúng. Đừng
# preload các lib CUDA (libnvrtc/cudnn…) để "sửa" cảnh báo onnxruntime — đó là fallback ĐÚNG.

import argparse
import contextlib
import fcntl
import subprocess
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
from color_correct_mouth import color_correct_mouth, composite_ai_mouth

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


def _probe_duration(path):
    """Media duration in seconds via ffprobe, or 0.0 if unknown."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float(out.stdout.strip())
    except Exception:
        return 0.0


def _trim_to_audio(video_path, audio_path, work_dir, margin=2.0):
    """If the carrier video is LONGER than the audio, physically trim it to ~audio length
    (stream-copy, near-instant) BEFORE the heavy steps (downscale / normalize_25fps / scenedetect)
    so those never re-encode the tail we discard anyway — the pipeline only renders audio-length.

    No-op if the video is shorter than (or near) the audio: that case is handled downstream by
    prepare_carrier (forward-loop + crossfade to audio length). +margin keeps the carrier safely
    longer than the audio (stream-copy cuts on keyframe boundaries) so the pipeline stays on the
    truncate path and never re-loops. Falls back to the original on any failure (correctness first).
    """
    vdur = _probe_duration(video_path)
    adur = _probe_duration(audio_path)
    if not vdur or not adur:
        return video_path
    keep = adur + margin
    if vdur <= keep:
        return video_path                                  # already short enough -> let prepare_carrier loop
    out = os.path.join(work_dir, "_trim.mp4")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-nostdin", "-i", video_path,
             "-t", f"{keep:.3f}", "-c", "copy", "-an", out],
            check=True,
        )
    except Exception as e:
        print(f"[render_job] trim failed ({e}); using original video", flush=True)
        return video_path
    if _probe_duration(out) < adur:                        # keyframe cut landed too short -> unsafe, skip
        return video_path
    print(f"[render_job] trim carrier {vdur:.1f}s -> ~{keep:.1f}s (audio {adur:.1f}s) before heavy steps", flush=True)
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
           guidance, steps, seed, enhance_mouth, enhance_region, out_res, color_correct=True,
           input_type="real"):
    work_dir = Path(output_path).parent
    work_dir.mkdir(parents=True, exist_ok=True)

    video_path = Path(video_path).absolute().as_posix()
    audio_path = Path(audio_path).absolute().as_posix()
    output_path = Path(output_path).absolute().as_posix()

    # Carrier longer than audio? Trim to ~audio length FIRST (cheap stream-copy) so the heavy
    # downscale / normalize / scenedetect below process ~audio-length, not the full clip. Shorter
    # videos pass through untouched -> prepare_carrier forward-loops + cuts them to audio length.
    video_path = _trim_to_audio(video_path, audio_path, str(work_dir))

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

    ai_mode = input_type == "ai"
    if ai_mode:
        guidance = min(float(guidance), 1.3)
        steps = max(int(steps), 28)
        print(f"[render_job] AI preset: guidance={guidance}, steps={steps}, narrow mouth composite, GFPGAN=off", flush=True)
    else:
        guidance = min(float(guidance), 1.5)
        steps = max(int(steps), 24)
        print(f"[render_job] REAL preset: guidance={guidance}, steps={steps}, overlap=4, "
              f"GFPGAN={bool(enhance_mouth)}", flush=True)
    print(f"[render_job] config={config_path} ckpt={checkpoint} gfpgan={enhance_mouth and not ai_mode}", flush=True)
    config = OmegaConf.load(config_path)
    config["run"].update({"guidance_scale": guidance, "inference_steps": steps})
    if ai_mode:
        # Overlap blends two independently generated mouths. On fast synthetic head motion their
        # geometry differs slightly, producing a visible soft/double mouth. Prefer crisp windows.
        config["run"]["chunk_overlap"] = 0
    else:
        config["run"]["chunk_overlap"] = 4

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

        if ai_mode:
            print("[render_job] AI conservative mouth composite", flush=True)
            composite_ai_mouth(raw_out, video_path, output_path, alpha=1.0,
                               margin_x=1.12, margin_y=1.18, feather=9, smooth=0.65,
                               detail_strength=0.42)
        elif enhance_mouth:
            try:
                torch.cuda.empty_cache()
            except Exception:
                pass
            print(f"[render_job] GFPGAN enhance (region={enhance_region}, {len(seams)} seam(s))", flush=True)
            restored_out = str(work_dir / "_mouth_restored.mp4") if color_correct else output_path
            restore_mouth(raw_out, restored_out, region=enhance_region, seams=seams,
                          gfpgan_alpha=0.5, sharpen=0.0, single_detect=False)
            if color_correct and enhance_region == "mouth":
                print("[render_job] color-correct mouth against carrier", flush=True)
                color_correct_mouth(restored_out, video_path, output_path, feather=25,
                                    curve_smooth=0.6, sharpen=0.0, temporal=0.7, seams=seams)
            elif restored_out != output_path:
                os.replace(restored_out, output_path)
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
    ap.add_argument("--color_correct", type=int, default=1, help="1=match mouth tone to carrier after GFPGAN")
    ap.add_argument("--input_type", choices=["real", "ai"], default="real")
    a = ap.parse_args()
    try:
        render(a.video, a.audio, a.output, a.config, a.checkpoint, a.guidance, a.steps,
               a.seed, bool(a.enhance_mouth), a.enhance_region, a.out_res, bool(a.color_correct), a.input_type)
    except Exception as e:
        print(f"[render_job] ERROR: {e}", file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
