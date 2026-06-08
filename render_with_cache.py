"""
Script 2 — render ONE clip from a precomputed avatar cache + an audio file.

Loads the cache (faces/affine/mouth) and runs ONLY the audio-dependent work: Whisper + Diffusion
+ GFPGAN. Skips detect/crop/affine/mouth-detect (served from cache).

  python render_with_cache.py --avatar_cache avatar_cache/{md5}/ --audio a.wav --output out.mp4
        [--steps 20] [--guidance 1.5] [--seed 1247] [--no_enhance]
"""
import argparse, os, time
import torch
torch.backends.cudnn.benchmark = True
from omegaconf import OmegaConf
from avatar_cache_lib import build_pipeline, load_cache, render_one, cache_is_complete


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--avatar_cache", required=True)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--steps", type=int, default=20)
    ap.add_argument("--guidance", type=float, default=1.5)
    ap.add_argument("--seed", type=int, default=1247)
    ap.add_argument("--no_enhance", action="store_true", help="skip GFPGAN mouth restore")
    ap.add_argument("--no_cache", action="store_true", help="compute affine/mouth in-line (A/B baseline)")
    ap.add_argument("--config", default="configs/unet/stage2_512.yaml")
    ap.add_argument("--ckpt", default="checkpoints/latentsync_unet.pt")
    a = ap.parse_args()

    if not cache_is_complete(a.avatar_cache):
        raise SystemExit(f"incomplete/missing cache: {a.avatar_cache}")
    if not os.path.exists(a.audio):
        raise SystemExit(f"audio not found: {a.audio}")

    config = OmegaConf.load(a.config)
    t = time.time()
    pipeline, dtype = build_pipeline(config, a.ckpt)
    t_load = time.time() - t
    cache = load_cache(a.avatar_cache)

    os.makedirs(os.path.dirname(os.path.abspath(a.output)), exist_ok=True)
    t = time.time()
    final, timings = render_one(pipeline, config, dtype, cache, a.audio, a.output,
                                steps=a.steps, guidance=a.guidance, seed=a.seed,
                                enhance=not a.no_enhance, use_cache=not a.no_cache)
    t_render = time.time() - t

    # make --output the final (enhanced) file; drop the intermediate lipsync file
    if final != a.output:
        os.replace(final, a.output)
    print(f"[render_with_cache] model_load={t_load:.1f}s render={t_render:.1f}s {timings}")
    print(f"Output: {a.output}")


if __name__ == "__main__":
    main()
