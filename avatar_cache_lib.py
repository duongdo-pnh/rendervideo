"""
Shared helpers for the avatar precompute-cache workflow (precompute_avatar.py,
render_with_cache.py, batch_render.py).

The cacheable work for a fixed avatar video is the per-frame insightface detect+align
(profiled ~26 ms/f) and the MediaPipe mouth detect — both depend ONLY on the video, not the
audio. We cache them once; every clip then skips them. The UNet denoise (the real cost, ~68%)
is audio-dependent and is NOT cached.

Cache layout:  avatar_cache/{md5_video}/
  frames_aligned.npz   faces: (N,3,512,512) uint8  — aligned 512 face crops
  face_data.pkl        boxes + metadata (abs video_path, md5, n_frames, resolution, H, W)
  affine_matrix.pkl    list[(2,3) float32]         — align affine per frame
  mouth_coords.pkl     {cx,cy,hw,hh, landmarks5}   — for restore_mouth (skip its MediaPipe)
"""
import os, hashlib, pickle, time
import numpy as np
import torch

CACHE_FILES = ("frames_aligned.npz", "face_data.pkl", "affine_matrix.pkl", "mouth_coords.pkl")


def video_md5(path, chunk=1 << 20):
    """MD5 of the video file bytes -> stable per-avatar cache key."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(chunk), b""):
            h.update(blk)
    return h.hexdigest()


def cache_dir_for(video_path, cache_root="avatar_cache"):
    return os.path.join(cache_root, video_md5(video_path))


def cache_is_complete(cache_dir):
    return all(os.path.exists(os.path.join(cache_dir, f)) for f in CACHE_FILES)


# ---------------------------------------------------------------- precompute side

def precompute_avatar(video_path, cache_root="avatar_cache", resolution=512):
    """Run the cacheable avatar preprocessing once and write the cache. Returns cache_dir."""
    from latentsync.utils.image_processor import ImageProcessor
    from latentsync.utils.util import read_video
    import cv2
    from restore_mouth_gfpgan import _detect_mouth

    cache_dir = cache_dir_for(video_path, cache_root)
    os.makedirs(cache_dir, exist_ok=True)

    # 1) affine/align on read_video frames — IDENTICAL path to the pipeline so cached faces match
    #    byte-for-byte (same color order, same sequential p_bias smoothing in align_warp_face).
    t = time.time()
    ip = ImageProcessor(resolution, device="cuda", mask_image=None)
    frames = read_video(video_path, use_decord=False)
    n = len(frames)
    faces, boxes, affines = [], [], []
    for i in range(n):
        face, box, aff = ip.affine_transform(frames[i])          # face (3,512,512) uint8 cpu
        faces.append(face)
        boxes.append(box)
        affines.append(aff.squeeze(0).float().cpu().numpy())     # (2,3) float32
        if (i + 1) % 100 == 0 or i == n - 1:
            print(f"  affine {i+1}/{n}", flush=True)
    faces = torch.stack(faces).numpy().astype(np.uint8)          # (N,3,512,512)
    t_affine = time.time() - t

    # 2) mouth detect on cv2-BGR frames — matches restore_mouth's input (it reads via cv2 BGR)
    t = time.time()
    cap = cv2.VideoCapture(video_path)
    bgr = []
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        bgr.append(fr)
    cap.release()
    H, W = bgr[0].shape[:2]
    cx, cy, hw, hh, lm5 = _detect_mouth(bgr, W, H)
    t_mouth = time.time() - t

    # 3) save
    t = time.time()
    np.savez_compressed(os.path.join(cache_dir, "frames_aligned.npz"), faces=faces)
    pickle.dump(
        {"boxes": boxes, "video_path": os.path.abspath(video_path), "md5": os.path.basename(cache_dir),
         "n_frames": n, "resolution": resolution, "H": H, "W": W},
        open(os.path.join(cache_dir, "face_data.pkl"), "wb"),
    )
    pickle.dump(affines, open(os.path.join(cache_dir, "affine_matrix.pkl"), "wb"))
    pickle.dump({"cx": cx, "cy": cy, "hw": hw, "hh": hh, "landmarks5": lm5},
                open(os.path.join(cache_dir, "mouth_coords.pkl"), "wb"))
    t_save = time.time() - t

    sz = sum(os.path.getsize(os.path.join(cache_dir, f)) for f in CACHE_FILES) / 1e6
    print(f"  [precompute] affine {t_affine:.1f}s | mouth {t_mouth:.1f}s | save {t_save:.1f}s | "
          f"{n} frames | cache {sz:.0f}MB")
    return cache_dir


# ---------------------------------------------------------------- render side

def load_cache(cache_dir):
    npz = np.load(os.path.join(cache_dir, "frames_aligned.npz"))
    faces = torch.from_numpy(npz["faces"])                       # (N,3,512,512) uint8 cpu
    face_data = pickle.load(open(os.path.join(cache_dir, "face_data.pkl"), "rb"))
    affines = pickle.load(open(os.path.join(cache_dir, "affine_matrix.pkl"), "rb"))
    mouth = pickle.load(open(os.path.join(cache_dir, "mouth_coords.pkl"), "rb"))
    return {"faces": faces, "boxes": face_data["boxes"], "affine": affines, "mouth": mouth, "meta": face_data}


def build_pipeline(config, ckpt_path="checkpoints/latentsync_unet.pt", enable_deepcache=True):
    """Construct VAE+UNet+Whisper+scheduler exactly like scripts/inference.main. Call ONCE and
    reuse across many clips (batch_render) to avoid ~30-60s model load per clip."""
    from omegaconf import OmegaConf
    from diffusers import AutoencoderKL, DDIMScheduler
    from latentsync.models.unet import UNet3DConditionModel
    from latentsync.pipelines.lipsync_pipeline import LipsyncPipeline
    from latentsync.whisper.audio2feature import Audio2Feature

    is_fp16 = torch.cuda.is_available() and torch.cuda.get_device_capability()[0] > 7
    dtype = torch.float16 if is_fp16 else torch.float32

    if os.environ.get("LATENTSYNC_SCHEDULER", "dpm").lower().startswith("dpm"):
        from diffusers import DPMSolverMultistepScheduler
        scheduler = DPMSolverMultistepScheduler.from_pretrained("configs", algorithm_type="dpmsolver++")
    else:
        scheduler = DDIMScheduler.from_pretrained("configs")

    whisper_path = "checkpoints/whisper/small.pt" if config.model.cross_attention_dim == 768 else "checkpoints/whisper/tiny.pt"
    audio_encoder = Audio2Feature(model_path=whisper_path, device="cuda",
                                  num_frames=config.data.num_frames, audio_feat_length=config.data.audio_feat_length)
    vae = AutoencoderKL.from_pretrained("stabilityai/sd-vae-ft-mse", torch_dtype=dtype)
    vae.config.scaling_factor = 0.18215
    vae.config.shift_factor = 0
    unet, _ = UNet3DConditionModel.from_pretrained(OmegaConf.to_container(config.model), ckpt_path, device="cpu")
    unet = unet.to(dtype=dtype)
    pipeline = LipsyncPipeline(vae=vae, audio_encoder=audio_encoder, unet=unet, scheduler=scheduler).to("cuda")
    if enable_deepcache:
        from DeepCache import DeepCacheSDHelper
        helper = DeepCacheSDHelper(pipe=pipeline)
        helper.set_params(cache_interval=3, cache_branch_id=0)
        helper.enable()
    return pipeline, dtype


def render_one(pipeline, config, dtype, cache, audio_path, output_path,
               steps=20, guidance=1.5, seed=1247, enhance=True, temp_dir="temp", use_cache=True):
    """Render one clip from a loaded cache + audio. Returns (final_path, timings dict).
    use_cache=False runs the SAME path but computes affine/mouth in-line (for cache-vs-nocache A/B)."""
    import cv2
    from accelerate.utils import set_seed
    if seed != -1:
        set_seed(seed)

    meta = cache["meta"]
    t = time.time()
    pipeline(
        video_path=meta["video_path"], audio_path=audio_path, video_out_path=output_path,
        num_frames=config.data.num_frames, num_inference_steps=steps, guidance_scale=guidance,
        weight_dtype=dtype, width=config.data.resolution, height=config.data.resolution,
        mask_image_path=config.data.mask_image_path, temp_dir=temp_dir,
        precomputed_faces=(cache["faces"] if use_cache else None),
        precomputed_boxes=(cache["boxes"] if use_cache else None),
        precomputed_affine_matrices=(cache["affine"] if use_cache else None),
    )
    t_diff = time.time() - t

    final = output_path
    t_enh = 0.0
    if enhance:
        from restore_mouth_gfpgan import restore_mouth
        precomputed_mouth = None
        if use_cache:
            # rendered output frame k corresponds to avatar frame (k % N) (pipeline forward-loops);
            # build per-frame mouth landmarks to match, so restore_mouth skips its MediaPipe pass.
            cap = cv2.VideoCapture(output_path)
            L = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            cap.release()
            m = cache["mouth"]
            lm5 = m["landmarks5"]
            N = len(lm5)
            looped = [lm5[k % N] for k in range(L)] if N else lm5
            precomputed_mouth = (m["cx"], m["cy"], m["hw"], m["hh"], looped)
        final = output_path.replace(".mp4", "_enhanced.mp4")
        t = time.time()
        restore_mouth(output_path, final, region="mouth", precomputed_mouth=precomputed_mouth)
        t_enh = time.time() - t

    return final, {"diffusion+restorevideo": t_diff, "enhance": t_enh}
