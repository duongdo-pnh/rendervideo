"""
Script 1 — precompute the reusable avatar cache (run ONCE per avatar video).

Caches the audio-independent work (insightface detect+align, mouth coords) so every later clip
rendered from this avatar skips it. See avatar_cache_lib for the cache layout.

  python precompute_avatar.py --video avatar.mp4 [--cache_root avatar_cache] [--resolution 512]

Output: "Cache saved: avatar_cache/{md5}/"
"""
import argparse, os, time
from avatar_cache_lib import precompute_avatar, cache_dir_for, cache_is_complete


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--cache_root", default="avatar_cache")
    ap.add_argument("--resolution", type=int, default=512)
    ap.add_argument("--force", action="store_true", help="recompute even if cache exists")
    a = ap.parse_args()

    if not os.path.exists(a.video):
        raise SystemExit(f"video not found: {a.video}")

    cache_dir = cache_dir_for(a.video, a.cache_root)
    if cache_is_complete(cache_dir) and not a.force:
        print(f"Cache already exists (use --force to recompute): {cache_dir}/")
        print(f"Cache saved: {cache_dir}/")
        return

    t0 = time.time()
    cache_dir = precompute_avatar(a.video, a.cache_root, a.resolution)
    print(f"Precompute time: {time.time()-t0:.1f}s")
    print(f"Cache saved: {cache_dir}/")


if __name__ == "__main__":
    main()
