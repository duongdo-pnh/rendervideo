"""
Script 3 — render ALL audio files in a folder against ONE avatar cache.

Builds the pipeline ONCE and reuses it across every clip (warm models -> saves ~30-60s model load
per clip, the biggest batch win) and serves face/affine/mouth from the cache.

  python batch_render.py --avatar_cache avatar_cache/{md5}/ --audio_dir ./audios/ --output_dir ./clips/
        [--steps 20] [--guidance 1.5] [--seed 1247] [--no_enhance] [--ext wav,mp3]

Outputs: clips/clip_001.mp4, clip_002.mp4, ... (sorted by audio filename)
"""
import argparse, os, time, glob
import torch
torch.backends.cudnn.benchmark = True
from omegaconf import OmegaConf
from avatar_cache_lib import build_pipeline, load_cache, render_one, cache_is_complete


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--avatar_cache", required=True)
    ap.add_argument("--audio_dir", required=True)
    ap.add_argument("--output_dir", required=True)
    ap.add_argument("--steps", type=int, default=20)
    ap.add_argument("--guidance", type=float, default=1.5)
    ap.add_argument("--seed", type=int, default=1247)
    ap.add_argument("--no_enhance", action="store_true")
    ap.add_argument("--ext", default="wav,mp3,m4a,flac", help="comma-separated audio extensions")
    ap.add_argument("--config", default="configs/unet/stage2_512.yaml")
    ap.add_argument("--ckpt", default="checkpoints/latentsync_unet.pt")
    a = ap.parse_args()

    if not cache_is_complete(a.avatar_cache):
        raise SystemExit(f"incomplete/missing cache: {a.avatar_cache}")
    exts = {e.strip().lower().lstrip(".") for e in a.ext.split(",")}
    audios = sorted(p for p in glob.glob(os.path.join(a.audio_dir, "*"))
                    if p.rsplit(".", 1)[-1].lower() in exts)
    if not audios:
        raise SystemExit(f"no audio ({a.ext}) found in {a.audio_dir}")
    os.makedirs(a.output_dir, exist_ok=True)
    print(f"[batch] {len(audios)} audio file(s) -> {a.output_dir}")

    t = time.time()
    config = OmegaConf.load(a.config)
    pipeline, dtype = build_pipeline(config, a.ckpt)          # ONCE — kept warm across all clips
    cache = load_cache(a.avatar_cache)                        # ONCE
    print(f"[batch] model load + cache load: {time.time()-t:.1f}s (one-time)")

    times = []
    for i, audio in enumerate(audios, 1):
        out = os.path.join(a.output_dir, f"clip_{i:03d}.mp4")
        t = time.time()
        final, timings = render_one(pipeline, config, dtype, cache, audio, out,
                                    steps=a.steps, guidance=a.guidance, seed=a.seed, enhance=not a.no_enhance)
        if final != out:
            os.replace(final, out)
        dt = time.time() - t
        times.append(dt)
        print(f"[batch] {i}/{len(audios)} {os.path.basename(audio)} -> {os.path.basename(out)}  {dt:.1f}s {timings}")

    print(f"[batch] DONE {len(times)} clips | total {sum(times):.1f}s | avg {sum(times)/len(times):.1f}s/clip "
          f"(model load amortized once)")


if __name__ == "__main__":
    main()
