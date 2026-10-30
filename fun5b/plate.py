"""
Clean plate of a reference image: the same scene with the person removed, for the "Nền sạch" box of the UI.

An instruction-following image editor, FireRed-Image-Edit-1.1 on this GPU, is asked to remove the person and
continue the background behind them. Compared on two references (a pharmacy with shelves of products, a studio wall
with a logo and a curved band) against the first local editor,
Qwen-Image-Edit (Aug 2025): it zoomed and re-framed the whole picture (mean difference outside the person 89.9/255 on
the pharmacy), so the fill came out as green smears and a broken cabinet, and it invented chairs on the wall. FireRed
keeps the framing (3.4) and continues shelves, backlights, counter and band cleanly; Qwen-Image-Edit-2511 was close but
invented a product box with gibberish text, LongCat-Image-Edit-Turbo was 20x faster but shifted tone and bent edges.
Mask inpainting was tried before all of them: LaMa blurs a hole as big as a person, SDXL Inpainting invents objects
and leaves a grey shadow of the person.
The prompt names no objects: given "shelves, panels, chairs and the table" the editors drew exactly those things.
Every pixel outside the person's mask (widened, as Matting.plate() does) stays the reference's own; the editor's
picture is warped onto the reference's geometry by an optical flow measured outside the mask and carried smoothly into
it, then blended into the hole by Poisson blending. Its raw picture is remembered per reference (cached()).
FireRed: ~6.5 min and ~19GB of the GPU, run in its own environment (.venv-edit, fun5b/edit_worker.py; the UI unloads
the video model meanwhile). Gemini's image model online was offered too and dropped: it needs billing on the key, and
one editor, the best, is kept.
"""

import argparse
import json
import os
import time

import cv2
import numpy as np
from PIL import Image, ImageOps

from fun5b import config, plate_key

FIRERED = os.path.join(config.CKPT_DIR, "firered-image-edit")
FIRERED_GGUF = os.path.join(FIRERED, "FireRed-Image-Edit-1.1-Q5_K_M.gguf")
EDIT_PYTHON = config.EDIT_PYTHON
EDIT_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "edit_worker.py")
from fun5b.plate_key import LOCAL_CFG, LOCAL_PROMPT, LOCAL_SIZE, LOCAL_STEPS, SEED  # noqa: E402
CACHE_DIR = config.PLATE_CACHE_DIR


def edit_image_local(image, timeout=1800, cancel=None):
    """PIL image -> FireRed's edited PIL image (768x1376), in a subprocess of .venv-edit (see fun5b/edit_worker.py).
    `cancel`: optional callable, checked every second; when true the worker is killed and EditCancelled raised."""
    import subprocess
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        src, out, err = os.path.join(tmp, "in.png"), os.path.join(tmp, "out.png"), os.path.join(tmp, "err.txt")
        image.save(src)
        with open(err, "w") as log:
            run = subprocess.Popen([EDIT_PYTHON, "-I", "-W", "ignore", EDIT_WORKER, "--model-dir", FIRERED, "--in", src,
                                    "--out", out, "--prompt", LOCAL_PROMPT, "--steps", str(LOCAL_STEPS),
                                    "--cfg", str(LOCAL_CFG), "--size", LOCAL_SIZE, "--seed", str(SEED)],
                                   stdout=subprocess.DEVNULL, stderr=log,
                                   env={**os.environ, "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True"})
            start = time.time()
            while run.poll() is None:
                if (cancel is not None and cancel()) or time.time() - start > timeout:
                    run.kill()
                    run.wait()
                    if cancel is not None and cancel():
                        raise EditCancelled
                    raise RuntimeError(f"FireRed quá {timeout}s")
                time.sleep(1)
        if run.returncode != 0 or not os.path.isfile(out):
            raise RuntimeError(f"FireRed lỗi (mã {run.returncode}): {open(err).read()[-600:]}")
        return Image.open(out).convert("RGB")


class EditCancelled(Exception):
    """The editor was stopped by the caller's cancel."""


def person_mask(reference, matting):
    """uint8 0/1 mask of the person in `reference`, widened like Matting.plate()'s."""
    matting.reset()
    for _ in range(6):  # let the recurrent state settle on the still image
        _, alpha = matting(reference)
    matting.reset()
    mask = (alpha[..., 0] > 0.02).astype(np.uint8)
    return cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (45, 45)))


def align(reference, edited, mask):
    """`edited` warped onto `reference`'s geometry: DIS optical flow measured away from the mask, extended into it."""
    from fun5b.matting import _pull_push

    gray = lambda x: cv2.cvtColor(x, cv2.COLOR_RGB2GRAY)  # noqa: E731
    flow = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM).calc(gray(reference), gray(edited), None)
    keep = cv2.erode((1 - mask).astype(np.uint8), np.ones((15, 15), np.uint8)).astype(np.float32)
    inside = _pull_push(np.dstack([flow, np.zeros_like(flow[..., :1])]), keep)[..., :2]
    flow = np.where(keep[..., None] > 0, flow, cv2.GaussianBlur(inside, (0, 0), 10))
    y, x = np.mgrid[0 : flow.shape[0], 0 : flow.shape[1]].astype(np.float32)
    return cv2.remap(edited, x + flow[..., 0], y + flow[..., 1], cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)


def merge(reference, edited, mask):
    """`reference` with the masked pixels from `edited` (already aligned), Poisson-blended at the mask's edge."""
    pad = 24  # seamlessClone needs the region away from the border
    ref = cv2.copyMakeBorder(reference, pad, pad, pad, pad, cv2.BORDER_REFLECT)
    new = cv2.copyMakeBorder(edited, pad, pad, pad, pad, cv2.BORDER_REFLECT)
    hole = cv2.copyMakeBorder(mask * 255, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=0)
    ys, xs = np.nonzero(hole)
    if len(ys) == 0:
        return reference
    center = (int((xs.min() + xs.max()) // 2), int((ys.min() + ys.max()) // 2))
    out = cv2.seamlessClone(new, ref, hole, center, cv2.NORMAL_CLONE)[pad:-pad, pad:-pad]
    keep = (mask == 0)[..., None]
    return np.where(keep, reference, out)  # outside the mask: the reference's own pixels, untouched


def compose(reference, raw, matting):
    """The clean plate: `reference` with the person's mask filled from the editor's picture `raw`."""
    mask = person_mask(reference, matting)
    return merge(reference, align(reference, raw, mask), mask)


def available():
    """Whether the editor can run: FireRed's weights and environment installed."""
    return os.path.isfile(FIRERED_GGUF) and os.path.isfile(EDIT_PYTHON)


def cache_key(reference):
    """Cache id of a fitted (640×1152 uint8) reference: the same picture always reuses its clean plate."""
    return plate_key.cache_key(reference.tobytes())


def _cache_path(reference):
    return os.path.join(CACHE_DIR, cache_key(reference) + ".png")


def _touch_meta(key, **fields):
    """<key>.json next to the plate: source_name, created_at, last_used (shown by the app's "Nền đã lưu" page)."""
    path = os.path.join(CACHE_DIR, key + ".json")
    try:
        with open(path) as f:
            meta = json.load(f)
    except (OSError, ValueError):
        meta = {}
    meta.update(fields)
    meta["last_used"] = time.strftime("%Y-%m-%d %H:%M:%S")
    meta.setdefault("created_at", meta["last_used"])
    with open(path + ".tmp", "w") as f:
        json.dump(meta, f, ensure_ascii=False)
    os.replace(path + ".tmp", path)


def cached(reference):
    """The editor's raw picture made before for this exact `reference`, or None."""
    path = _cache_path(reference)
    if not os.path.isfile(path):
        return None
    _touch_meta(cache_key(reference))
    return np.asarray(Image.open(path).convert("RGB"))


def make(reference, cancel=None, source_name=None):
    """The editor's raw picture for `reference` (remembered for the next render of it, with a thumbnail of the
    reference as <key>_source.jpg). `cancel`: see edit_image_local."""
    edited = edit_image_local(Image.fromarray(reference), cancel=cancel)
    raw = np.asarray(edited.convert("RGB").resize(reference.shape[1::-1], Image.LANCZOS))
    os.makedirs(CACHE_DIR, exist_ok=True)
    key = cache_key(reference)
    Image.fromarray(raw).save(_cache_path(reference))
    Image.fromarray(reference).save(os.path.join(CACHE_DIR, key + "_source.jpg"), quality=88)
    _touch_meta(key, source_name=source_name)
    return raw


def main():
    from fun5b.run import RENDER_H, RENDER_W

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", help="the character image (the UI's reference)")
    parser.add_argument("--out", help="default: <image>_clean.png next to it")
    args = parser.parse_args()
    out = args.out or os.path.splitext(args.image)[0] + "_clean.png"
    reference = np.asarray(ImageOps.fit(Image.open(args.image).convert("RGB"), (RENDER_W, RENDER_H), Image.LANCZOS))
    tic = time.time()
    from fun5b.matting import Matting

    raw = cached(reference)
    raw = make(reference) if raw is None else raw
    Image.fromarray(compose(reference, raw, Matting())).save(out)
    print(f"Saved {out} ({RENDER_W}x{RENDER_H}) in {time.time() - tic:.0f}s")


if __name__ == "__main__":
    main()
