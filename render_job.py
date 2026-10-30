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

# Long single-process renders (512, many chunks) grow "reserved but unallocated" CUDA memory
# from allocator fragmentation until an alloc that would otherwise fit fails. Musetalk's
# persistent server also holds ~7.3GB on the same card the whole time, so headroom is tight.
# expandable_segments lets the allocator grow/shrink a segment instead of hunting for a new
# contiguous block, which is exactly the failure mode in the OOM traces (queue_worker.log:
# "Of the allocated memory 8.26 GiB is allocated ... 4.6+ GiB is reserved but unallocated").
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

# RTX 5090 profile (2026-07-23): keep Torch cu128/sm_120 and DO NOT enable xformers unless
# a wheel explicitly supports Blackwell. Face detect/align should use ONNXRuntime GPU; verify with:
#   import onnxruntime as ort; print(ort.get_available_providers())
# Expected providers include CUDAExecutionProvider. Keep protobuf<5 for mediapipe/GFPGAN restore.

import argparse
import contextlib
import fcntl
import subprocess
import time
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

from extend_video import prepare_carrier

# Output resolution presets (target SHORTER side). None = keep source.
OUT_RES = {"Gốc": None, "1080": 1080, "720": 720, "480 Nhanh": 480}


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
    nvenc = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", video_path, "-vf", vf,
         "-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ll", "-cq", "20",
         "-pix_fmt", "yuv420p", "-an", out],
        capture_output=True, text=True,
    )
    if nvenc.returncode != 0:
        print(f"[render_job] NVENC downscale failed; CPU fallback: {nvenc.stderr[-200:]}", flush=True)
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", video_path, "-vf", vf,
             "-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-an", out],
            check=True,
        )
    print(f"[render_job] downscale {w}x{h} -> shorter side {target_short}", flush=True)
    return out


# LatentSync diffuses at up to this shorter side even when a smaller output is chosen, then
# downscales at the end (_finalize_downscale). Downscaling first loses detail through the
# fast NVENC pass and the later re-encodes; the 512px face crop makes 1080 cost ~the same.
LATENTSYNC_WORK_SHORT = 1080
# Mild unsharp after the lanczos downscale: 720 output measured ~1.1-1.2x the face sharpness
# of the source downscaled to 720 (vs ~0.65x before), without visible halos at this amount.
FINAL_UNSHARP = "unsharp=5:5:0.45:5:5:0"


def _finalize_downscale(path, target_short):
    """Lanczos-downscale the finished render to target shorter side + mild sharpen, in place."""
    cap = cv2.VideoCapture(path)
    w, h = int(cap.get(3)), int(cap.get(4))
    cap.release()
    if min(w, h) <= target_short:
        return
    tmp = path + ".final.mp4"
    vf = (f"scale='if(gt(iw,ih),-2,{target_short})':'if(gt(iw,ih),{target_short},-2)'"
          f":flags=lanczos,{FINAL_UNSHARP}")
    # Same targeted bitrate as the pipeline encode (see lipsync_pipeline.py).
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-nostdin", "-i", path, "-vf", vf,
         "-c:v", "libx264", "-preset", "medium", "-b:v", "2900k", "-maxrate", "3200k",
         "-bufsize", "3200k", "-pix_fmt", "yuv420p", "-c:a", "copy", tmp],
        check=True,
    )
    os.replace(tmp, path)
    print(f"[render_job] final downscale {w}x{h} -> shorter side {target_short} (lanczos + sharpen)",
          flush=True)


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
           input_type="real", engine="musetalk"):
    work_dir = Path(output_path).parent
    work_dir.mkdir(parents=True, exist_ok=True)

    video_path = Path(video_path).absolute().as_posix()
    audio_path = Path(audio_path).absolute().as_posix()
    output_path = Path(output_path).absolute().as_posix()

    # MuseTalk cache identity must depend only on video, never on audio duration.
    # LatentSync still benefits from trimming a long carrier before diffusion.
    if engine == "latentsync":
        video_path = _trim_to_audio(video_path, audio_path, str(work_dir))

    # Output resolution: downscale input to chosen shorter side (output res = input res).
    # LatentSync below 1080 diffuses at 1080 and downscales the result at the end instead.
    target_short = OUT_RES.get(out_res)
    final_short = None
    if engine == "latentsync" and target_short and target_short < LATENTSYNC_WORK_SHORT:
        final_short, target_short = target_short, LATENTSYNC_WORK_SHORT
    video_path = _maybe_downscale(video_path, target_short, str(work_dir))

    # The resident Fun-5B server (~12GB VRAM, ~14GB RAM) and this engine do not fit together.
    import fun5b_adapter
    fun5b_adapter.ensure_stopped()

    if engine == "musetalk":
        from musetalk_adapter import render as musetalk_render
        print("[render_job] engine=MuseTalk 1.5 (fp16)", flush=True)
        musetalk_render(video_path, audio_path, output_path, work_dir)
        if not os.path.exists(output_path):
            raise RuntimeError(f"render finished but output missing: {output_path}")
        print(f"[render_job] OK -> {output_path}", flush=True)
        return

    # LatentSync needs a carrier at least as long as the audio; MuseTalk cycles its cached avatar.
    ext_path = str(work_dir / "_carrier_ext.mp4")
    try:
        video_path, seams = prepare_carrier(video_path, audio_path, ext_path)
    except Exception as e:
        print(f"[render_job] prepare_carrier failed ({e}); using original video", flush=True)
        seams = []

    from omegaconf import OmegaConf
    from scripts.inference import main as inference_main
    from restore_mouth_gfpgan import restore_mouth
    from color_correct_mouth import color_correct_mouth, composite_ai_mouth
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

    # Serialize the GPU-heavy section across processes (diffusion + GFPGAN). On low-VRAM cards
    # (RTX 3080 Ti) LatentSync and the resident MuseTalk server don't fit together, so stop
    # MuseTalk for the duration; its next job restarts it.
    import gpu_info
    pause_musetalk = contextlib.nullcontext()
    if gpu_info.is_low_vram_gpu():
        from musetalk_adapter import paused as pause_musetalk_server
        pause_musetalk = pause_musetalk_server()
    with gpu_lock(), pause_musetalk:
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

    if final_short and os.path.exists(output_path):
        _finalize_downscale(output_path, final_short)

    if not os.path.exists(output_path):
        raise RuntimeError(f"render finished but output missing: {output_path}")
    print(f"[render_job] OK -> {output_path}", flush=True)


EXIT_REJECTED = 3  # the job's input is bad (no person found, ...): queue_worker fails it without retrying


# One progress scale for a whole Fun-5B job (jobs.progress, 0..1), so the queue UI and akali's
# heartbeat move during the minutes of model load / FireRed before the first denoising step too.
# 0.97 = rendered, uploading to Drive (akali_bridge); 1 = done.
FUN5B_STAGE_PROGRESS = {"load": 0.01, "plate": 0.03, "pose": 0.05}
FUN5B_RENDER_START, FUN5B_RENDER_SPAN = 0.05, 0.90


def render_fun5b(job_id, image, video, output_path, preset, seed, keep_background, auto_background,
                 table_line, sharp_hands, background):
    """Fun-5B character animation (ref image + driving video, no audio) via fun5b_adapter's
    persistent server, with the MuseTalk server stopped for the duration and the GPU lock held."""
    import database as db
    import fun5b_adapter
    from musetalk_adapter import paused as pause_musetalk_server

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    last, best = [0.0], [0.0]

    def on_progress(event):
        stage = event.get("stage")
        if stage == "render":
            value = FUN5B_RENDER_START + FUN5B_RENDER_SPAN * float(event.get("progress", 0))
        elif stage in FUN5B_STAGE_PROGRESS:
            value = FUN5B_STAGE_PROGRESS[stage]
        else:
            return
        if value <= best[0]:  # never backwards (the model reloads after FireRed: "load" again)
            return
        best[0] = value
        now = time.monotonic()
        if stage != "render" or now - last[0] >= 5:  # the queue UI polls every 2s; no need to write every step
            last[0] = now
            try:
                db.set_progress(job_id, value)
            except Exception as e:  # a locked/missing DB must never fail a 30-minute render
                print(f"[render_job] progress not saved: {e}", flush=True)

    print(f"[render_job] engine=Fun-5B preset={preset} seed={seed} keep_background={keep_background} "
          f"auto_background={auto_background} table_line={table_line} sharp_hands={sharp_hands}", flush=True)
    with gpu_lock(), pause_musetalk_server():
        fun5b_adapter.render(job_id, image, video, output_path, preset=preset, seed=seed,
                             keep_background=keep_background, auto_background=auto_background,
                             table_line=table_line, sharp_hands=sharp_hands, background_path=background,
                             on_progress=on_progress)
    if not os.path.exists(output_path):
        raise RuntimeError(f"render finished but output missing: {output_path}")
    print(f"[render_job] OK -> {output_path}", flush=True)


def main_fun5b(argv):
    import fun5b_adapter

    ap = argparse.ArgumentParser(description="Single-job Fun-5B render runner")
    ap.add_argument("--engine", choices=["fun5b"], required=True)
    ap.add_argument("--job-id", type=int, required=True)
    ap.add_argument("--ref-image", required=True)
    ap.add_argument("--video", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--preset", default="natural")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--keep-background", type=int, default=1)
    ap.add_argument("--auto-background", type=int, default=1)
    ap.add_argument("--table-line", type=float, default=None)
    ap.add_argument("--sharp-hands", type=int, default=0)
    ap.add_argument("--background", default=None)
    a = ap.parse_args(argv)
    try:
        render_fun5b(a.job_id, a.ref_image, a.video, a.output, a.preset, a.seed, bool(a.keep_background),
                     bool(a.auto_background), a.table_line, bool(a.sharp_hands), a.background)
    except fun5b_adapter.RenderRejected as e:
        print(f"[render_job] REJECTED: {e}", file=sys.stderr, flush=True)
        sys.exit(EXIT_REJECTED)
    except Exception as e:
        print(f"[render_job] ERROR: {e}", file=sys.stderr, flush=True)
        sys.exit(1)


def main():
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--engine")
    if pre.parse_known_args()[0].engine == "fun5b":  # different inputs (image, no audio): own CLI
        return main_fun5b(sys.argv[1:])
    ap = argparse.ArgumentParser(description="Single-job lip-sync render runner")
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
    ap.add_argument("--engine", choices=["musetalk", "latentsync"], default="musetalk")
    a = ap.parse_args()
    try:
        render(a.video, a.audio, a.output, a.config, a.checkpoint, a.guidance, a.steps,
               a.seed, bool(a.enhance_mouth), a.enhance_region, a.out_res, bool(a.color_correct), a.input_type, a.engine)
    except Exception as e:
        print(f"[render_job] ERROR: {e}", file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
