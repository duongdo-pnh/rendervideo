import gradio as gr
from pathlib import Path
import torch
# Hardware accel: autotune cuDNN conv algorithms for the fixed (16, 512, 512) chunk shape.
torch.backends.cudnn.benchmark = True
from scripts.inference import main
from omegaconf import OmegaConf
import argparse
import re
import os
import subprocess
import cv2
from datetime import datetime

from restore_mouth_gfpgan import restore_mouth
from extend_video import prepare_carrier
from color_correct_mouth import color_correct_mouth
from latentsync.utils.runtime import CANCEL, LatentSyncCancelled

# output resolution choices (target SHORTER side). Only downscales (never upscales).
OUT_RES = {"Gốc": None, "1080": 1080, "720": 720}


def _maybe_downscale(video_path, target_short, work_dir):
    """Downscale the input to target shorter-side (keep aspect, even dims) so the OUTPUT is that res.
    LatentSync output res = input res, so this is how we control output quality. No-op if already <= target."""
    if not target_short:
        return video_path
    cap = cv2.VideoCapture(video_path)
    w, h = int(cap.get(3)), int(cap.get(4))
    cap.release()
    if min(w, h) <= target_short:
        return video_path                                  # already small enough — don't upscale
    out = os.path.join(work_dir, f"_in_{target_short}p.mp4")
    vf = f"scale='if(gt(iw,ih),-2,{target_short})':'if(gt(iw,ih),{target_short},-2)'"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", video_path, "-vf", vf,
                    "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-an", out], check=True)
    print(f"downscale input {w}x{h} -> shorter side {target_short} ({out})")
    return out

# Two models the user can pick: 512 (v1.6 — nét/tự nhiên, chậm) vs 256 (v1.5 — nhanh ~2×, hơi kém
# tự nhiên trên mặt close-up nhưng ổn cho video mặt nhỏ). (config_path, checkpoint_path) per choice.
MODELS = {
    "512": ("configs/unet/stage2_512.yaml", "checkpoints/latentsync_unet.pt"),
    "256": ("configs/unet/stage2.yaml", "checkpoints/v1.5/latentsync_unet.pt"),
}
CONFIG_PATH = Path("configs/unet/stage2_512.yaml")
CHECKPOINT_PATH = Path("checkpoints/latentsync_unet.pt")


def process_video(
    video_path,
    audio_path,
    guidance_scale,
    inference_steps,
    seed,
    enhance_mouth,
    enhance_region,
    model_res="512",
    out_res="Gốc",
):
    use_gfpgan = bool(enhance_mouth)
    CANCEL.clear()  # fresh run — clear any stale cancel flag from a previous job
    print(f"512/v1.6 | gfpgan={use_gfpgan}")
    # Write results to ./output (NOT ./temp — the pipeline wipes temp_dir on every run,
    # see lipsync_pipeline.py:468-470, which would delete our output mid-pipeline).
    output_dir = Path("./output")
    output_dir.mkdir(parents=True, exist_ok=True)

    # Convert paths to absolute Path objects and normalize them
    video_file_path = Path(video_path)
    video_path = video_file_path.absolute().as_posix()
    audio_path = Path(audio_path).absolute().as_posix()

    # Output resolution: downscale the input to the chosen shorter-side (output res = input res).
    video_path = _maybe_downscale(video_path, OUT_RES.get(out_res), str(output_dir))

    current_time = datetime.now().strftime("%Y%m%d_%H%M%S")
    # Sanitize the uploaded filename's stem (spaces/special chars break the un-quoted ffmpeg shell
    # commands downstream, e.g. lipsync_pipeline mux on video_out_path). Derive ALL output paths from this.
    safe_stem = re.sub(r"[^\w.-]+", "_", video_file_path.stem) or "video"
    # Set the output path for the processed video
    output_path = str(output_dir / f"{safe_stem}_{current_time}.mp4")

    # If the video is shorter than the audio, smoothly extend it (forward-loop + crossfade,
    # no backwards motion) so the pipeline never ping-pongs. Also returns seam indices (loop
    # wraps + hard scene cuts) so the mouth temporal smooth won't ghost across discontinuities.
    ext_path = str(output_dir / f"{safe_stem}_{current_time}_ext.mp4")
    try:
        video_path, seams = prepare_carrier(video_path, audio_path, ext_path)
    except Exception as e:
        print(f"prepare_carrier failed ({e}); using original video")
        seams = []

    config_path, ckpt_path = MODELS.get(str(model_res), MODELS["512"])
    print(f"model_res={model_res} | config={config_path} ckpt={ckpt_path} | gfpgan={use_gfpgan}")
    config = OmegaConf.load(config_path)

    config["run"].update(
        {
            "guidance_scale": guidance_scale,
            "inference_steps": inference_steps,
        }
    )

    # Parse the arguments (route checkpoint per chosen model)
    args = create_args(video_path, audio_path, output_path, inference_steps, guidance_scale, seed,
                       ckpt_path=ckpt_path)

    try:
        result = main(
            config=config,
            args=args,
        )
        print("Lip-sync completed.")

        if use_gfpgan:
            # Free the diffusion allocator cache before loading GFPGAN, then
            # sharpen the (structurally blurred) mouth via GFPGAN face restoration.
            try:
                import torch
                torch.cuda.empty_cache()
            except Exception:
                pass
            restored_path = output_path.replace(".mp4", "_enhanced.mp4")
            print(f"Enhancing mouth GFPGAN (region={enhance_region}, {len(seams)} seam(s)) ...")
            restore_mouth(output_path, restored_path, region=enhance_region, seams=seams,
                          gfpgan_alpha=0.7, sharpen=0.0)
            print("Processing completed successfully.")
            return restored_path

        # GFPGAN off = FAST mode: chỉ trả raw diffusion output, BỎ color_correct (tiết kiệm ~166s).
        # Dùng cho video chào giá / không quan trọng — chấp nhận môi mềm để đổi tốc độ.
        print("GFPGAN off -> raw diffusion only (fast mode). Processing completed successfully.")
        return output_path
    except LatentSyncCancelled:
        # user pressed Hủy — stop cleanly, no error popup. Buttons reset via the .then chain.
        print("Process cancelled by user.")
        return None
    except Exception as e:
        print(f"Error during processing: {str(e)}")
        gr.Warning(f"Lỗi xử lý: {str(e)}")  # toast (non-blocking) so the .then chain still resets the UI
        return None



def create_args(
    video_path: str, audio_path: str, output_path: str, inference_steps: int, guidance_scale: float, seed: int,
    ckpt_path: str = None,
) -> argparse.Namespace:
    ckpt_path = ckpt_path or CHECKPOINT_PATH.absolute().as_posix()
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

    return parser.parse_args(
        [
            "--inference_ckpt_path",
            ckpt_path,
            "--video_path",
            video_path,
            "--audio_path",
            audio_path,
            "--video_out_path",
            output_path,
            "--inference_steps",
            str(inference_steps),
            "--guidance_scale",
            str(guidance_scale),
            "--seed",
            str(seed),
            "--temp_dir",
            "temp",
            "--enable_deepcache",
        ]
    )


# Create Gradio interface
with gr.Blocks(title="LatentSync demo") as demo:
    gr.Markdown(
        """
    <h1 align="center">LatentSync</h1>

    <div style="display:flex;justify-content:center;column-gap:4px;">
        <a href="https://github.com/bytedance/LatentSync">
            <img src='https://img.shields.io/badge/GitHub-Repo-blue'>
        </a> 
        <a href="https://arxiv.org/abs/2412.09262">
            <img src='https://img.shields.io/badge/arXiv-Paper-red'>
        </a>
    </div>
    """
    )

    with gr.Row():
        with gr.Column():
            video_input = gr.Video(label="Input Video")
            audio_input = gr.Audio(label="Input Audio", type="filepath")

            with gr.Row():
                guidance_scale = gr.Slider(
                    minimum=1.0,
                    maximum=3.0,
                    value=1.5,
                    step=0.1,
                    label="Guidance Scale",
                )
                inference_steps = gr.Slider(minimum=8, maximum=50, value=20, step=1, label="Inference Steps (DPM-Solver; 20 khuyến nghị — <15 nhép môi dễ lệch)")

            with gr.Row():
                seed = gr.Number(value=1247, label="Random Seed", precision=0)

            with gr.Row():
                enhance_mouth = gr.Checkbox(value=True, label="Làm nét miệng GFPGAN (BẬT = nét, +~1.2′ | TẮT = CHỈ diffusion, nhanh nhất, môi mềm — cho video chào giá không quan trọng)")
                enhance_region = gr.Radio(
                    choices=["mouth", "face"],
                    value="mouth",
                    label="Vùng làm nét (mouth = chỉ miệng, face = toàn mặt)",
                )

            with gr.Row():
                model_res = gr.Radio(
                    choices=["512", "256"],
                    value="512",
                    label="Model: 512 (nét/tự nhiên, chậm) | 256 (nhanh ~2×, hợp video mặt nhỏ — close-up có thể hơi giả)",
                )
                out_res = gr.Radio(
                    choices=["Gốc", "1080", "720"],
                    value="1080",
                    label="Độ phân giải output (cạnh ngắn): Gốc | 1080 | 720 (720 nhẹ + nhanh hơn ~15-20%, hợp live sales)",
                )

            with gr.Row():
                process_btn = gr.Button("Process Video", variant="primary")
                stop_btn = gr.Button("⏹ Hủy", variant="stop", visible=False)
            status = gr.Markdown(visible=False)

        with gr.Column():
            video_output = gr.Video(label="Output Video")

            gr.Examples(
                examples=[
                    ["assets/demo1_video.mp4", "assets/demo1_audio.wav"],
                    ["assets/demo2_video.mp4", "assets/demo2_audio.wav"],
                    ["assets/demo3_video.mp4", "assets/demo3_audio.wav"],
                ],
                inputs=[video_input, audio_input],
            )

    def _to_running():
        # toggle: hide Process, show Hủy, show running status. Clear the cancel flag for the new run.
        CANCEL.clear()
        return (gr.update(visible=False), gr.update(visible=True),
                gr.update(value="⏳ Đang xử lý... (ấn **Hủy** để dừng — đừng reload trang)", visible=True))

    def _to_idle():
        return (gr.update(visible=True), gr.update(visible=False), gr.update(visible=False))

    def _on_cancel():
        CANCEL.set()  # cooperative stop: pipeline/GFPGAN loops abort at the next chunk/batch
        return (gr.update(visible=True), gr.update(visible=False),
                gr.update(value="⛔ Đã hủy.", visible=True))

    # queue=False on the toggle/cancel handlers is CRITICAL: otherwise the Hủy click waits in the
    # queue behind the running process_video event and can never set the cancel flag mid-run.
    ev_start = process_btn.click(_to_running, outputs=[process_btn, stop_btn, status], queue=False)
    ev_run = ev_start.then(
        fn=process_video,
        inputs=[
            video_input,
            audio_input,
            guidance_scale,
            inference_steps,
            seed,
            enhance_mouth,
            enhance_region,
            model_res,
            out_res,
        ],
        outputs=video_output,
    )
    ev_run.then(_to_idle, outputs=[process_btn, stop_btn, status], queue=False)
    # Second click (now the visible button is "Hủy") runs OUTSIDE the queue so it sets the cooperative
    # flag immediately (GPU stops ~one chunk later) and also cancels the queued event; then resets UI.
    stop_btn.click(_on_cancel, outputs=[process_btn, stop_btn, status], cancels=[ev_run], queue=False)

if __name__ == "__main__":
    demo.queue()  # required for cancels=[...] (the Hủy button) to interrupt a running event
    demo.launch(inbrowser=True, share=True)
