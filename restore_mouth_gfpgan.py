"""
Sharpen the (structurally blurred) mouth of a LatentSync output using GFPGAN face
restoration, blended ONLY over the mouth region so eyes/skin/identity stay as LatentSync
produced them. Per-frame GFPGAN flicker is tamed with a light temporal smooth.

CLI:
  python restore_mouth_gfpgan.py --in IN.mp4 --out OUT.mp4 \
      [--region mouth|face] [--gfpgan_alpha 1.0] [--temporal 0.5] [--margin 1.6]

Importable:
  from restore_mouth_gfpgan import restore_mouth
  restore_mouth(in_path, out_path, region="mouth")
"""
import argparse, os, shutil, subprocess, sys, time
import numpy as np
import cv2
from latentsync.utils.runtime import CANCEL, LatentSyncCancelled

GFPGAN_WEIGHTS = "gfpgan/weights/GFPGANv1.4.pth"


def _ffmpeg_bin():
    """Resolve ffmpeg: PATH first, else next to the running python (conda env bin) so the CLI
    works standalone, not only when invoked from an activated env."""
    return shutil.which("ffmpeg") or os.path.join(os.path.dirname(sys.executable), "ffmpeg")

LIP_IDX = sorted(set([
    61,146,91,181,84,17,314,405,321,375,291,308,324,318,402,317,14,87,178,88,95,
    185,40,39,37,0,267,269,270,409,415,310,311,312,13,82,81,80,191,
    400,377,152,148,176,
]))

_RESTORER = None


def get_restorer():
    """Lazily build a single GFPGANer (reused across calls / Gradio requests)."""
    global _RESTORER
    if _RESTORER is None:
        from gfpgan import GFPGANer
        _RESTORER = GFPGANer(model_path=GFPGAN_WEIGHTS, upscale=1, arch="clean",
                             channel_multiplier=2, bg_upsampler=None)
    return _RESTORER


def _detect_mouth_one(fd, fm, f, W, H):
    """Detect one frame's mouth ellipse + arcface 5-point landmarks (full-image coords).
    Returns (center|None, hw|None, hh|None, lm5|None). center/hw/hh are None when no face."""
    rgb = cv2.cvtColor(f, cv2.COLOR_BGR2RGB)
    det = fd.process(rgb)
    if not det.detections:
        return None, None, None, None
    bb = det.detections[0].location_data.relative_bounding_box
    bx, by, bw, bh = bb.xmin*W, bb.ymin*H, bb.width*W, bb.height*H
    pad = bh * 0.5                                   # generous pad so chin/forehead stay in the crop
    x0, y0 = max(0, int(bx-pad)), max(0, int(by-pad))
    x1, y1 = min(W, int(bx+bw+pad)), min(H, int(by+bh+pad))
    cw, ch = x1-x0, y1-y0
    if cw < 8 or ch < 8:
        return None, None, None, None
    res = fm.process(rgb[y0:y1, x0:x1])              # FaceMesh on the zoomed face crop
    if not res.multi_face_landmarks:
        return None, None, None, None
    lm = res.multi_face_landmarks[0].landmark        # normalized to the CROP -> map back to full image
    xs = np.array([x0 + lm[j].x*cw for j in LIP_IDX])
    ys = np.array([y0 + lm[j].y*ch for j in LIP_IDX])
    center = ((xs.min()+xs.max())/2, (ys.min()+ys.max())/2)
    lm5 = np.array([[x0+lm[468].x*cw, y0+lm[468].y*ch], [x0+lm[473].x*cw, y0+lm[473].y*ch],
                    [x0+lm[1].x*cw, y0+lm[1].y*ch], [x0+lm[61].x*cw, y0+lm[61].y*ch],
                    [x0+lm[291].x*cw, y0+lm[291].y*ch]], dtype=np.float32)
    return center, (xs.max()-xs.min())/2, (ys.max()-ys.min())/2, lm5


def _make_mediapipe():
    import mediapipe as mp
    fd = mp.solutions.face_detection.FaceDetection(model_selection=1, min_detection_confidence=0.3)
    fm = mp.solutions.face_mesh.FaceMesh(static_image_mode=False, max_num_faces=1,
                                         refine_landmarks=True, min_detection_confidence=0.5)
    return fd, fm


def _detect_mouth(frames, W, H):
    """Per-frame: full-range FaceDetection locates the (possibly SMALL) face, we crop+zoom to it,
    then run FaceMesh on the crop -> mouth ellipse + arcface 5-point landmarks (mapped back to full
    image coords). The crop step is what makes this work on wide product-sales shots where the face
    is a tiny fraction of the frame — plain FaceMesh's short-range detector misses those entirely
    (insightface finds them for diffusion, but FaceMesh did not, so restore_mouth used to crash with
    'no face found'). arcface order: [right-iris 468, left-iris 473, nose 1, left-mouth 61, right-mouth 291]."""
    fd, fm = _make_mediapipe()
    centers, hw, hh, landmarks5 = [], [], [], []
    for f in frames:
        center, w, h, lm5 = _detect_mouth_one(fd, fm, f, W, H)
        landmarks5.append(lm5)
        if center is not None:
            centers.append(center); hw.append(w); hh.append(h)
    fd.close(); fm.close()
    if not centers:
        raise RuntimeError("no face found for mouth detection")
    c = np.array(centers)
    return int(np.median(c[:,0])), int(np.median(c[:,1])), int(np.median(hw)), int(np.median(hh)), landmarks5


GFPGAN_BATCH = 8


def _gfpgan_restore_batched(restorer, frames, landmarks5, batch_size=GFPGAN_BATCH, bbox=None):
    """GFPGAN restore, but using MediaPipe 5-point landmarks (passed in) for alignment instead of
    GFPGAN's built-in RetinaFace (~76ms/frame — the profiled bottleneck). Alignment template +
    GAN are unchanged so quality is preserved. GAN runs batched. Returns full-frame BGR images.

    `bbox=(x0,y0,x1,y1)` (mouth mode): paste the restored face back with a direct inverse warpAffine
    into ONLY that box instead of GFPGAN's paste_faces_to_input_image (profiled at 164ms/frame — the
    real bottleneck: it inverse-warps the full canvas, builds+blurs its own face mask, and re-reads
    the frame, all of which restore_mouth's ellipse mask discards). Same restored pixels, ~80x cheaper
    paste; outside `bbox` the frame is left untouched (the caller's ellipse mask lives inside it).
    bbox=None (face mode): keep the soft full-frame paste so the whole face blends into the bg."""
    import torch
    from basicsr.utils import img2tensor, tensor2img
    from torchvision.transforms.functional import normalize
    fh = restorer.face_helper
    dev = restorer.device
    n = len(frames)
    crops, affines, ok = [], [], []
    # Phase 1 — align each frame from the MediaPipe 5-point (no RetinaFace), keep aligned face + affine
    for i, f in enumerate(frames):
        if landmarks5[i] is None:
            crops.append(None); affines.append(None); ok.append(False)
            continue
        fh.clean_all()
        fh.read_image(f)
        fh.all_landmarks_5 = [landmarks5[i]]
        fh.align_warp_face()
        if len(fh.cropped_faces) > 0:
            crops.append(fh.cropped_faces[0]); affines.append(list(fh.affine_matrices)); ok.append(True)
        else:
            crops.append(None); affines.append(None); ok.append(False)
    # Phase 2 — batched GAN forward over the aligned faces
    restored = [None] * n
    idxs = [i for i in range(n) if ok[i]]
    for b in range(0, len(idxs), batch_size):
        ch = idxs[b:b + batch_size]
        ts = []
        for i in ch:
            t = img2tensor(crops[i] / 255., bgr2rgb=True, float32=True)
            normalize(t, (0.5, 0.5, 0.5), (0.5, 0.5, 0.5), inplace=True)
            ts.append(t)
        batch = torch.stack(ts).to(dev)
        with torch.no_grad():
            out_b = restorer.gfpgan(batch, return_rgb=False, weight=0.5)[0]
        for k, i in enumerate(ch):
            restored[i] = tensor2img(out_b[k], rgb2bgr=True, min_max=(-1, 1)).astype("uint8")
        print(f"[restore] {min(b + batch_size, len(idxs))}/{len(idxs)} (batch)")
    # Phase 3 — paste each restored face back into its frame via the saved affine
    results = []
    if bbox is not None:
        # FAST mouth path: inverse-warp the restored 512 face directly into the mouth bbox only.
        x0, y0, x1, y1 = bbox
        bw, bh = x1 - x0, y1 - y0
        ones = np.ones(restored[idxs[0]].shape[:2], np.uint8) if idxs else None  # probe: where the warp covers
        ker = np.ones((3, 3), np.uint8)
        for i, f in enumerate(frames):
            if not ok[i]:
                results.append(f); continue
            aff = np.asarray(affines[i][0], dtype=np.float64)         # image->aligned (512) forward warp
            inv = cv2.invertAffineTransform(aff)                      # aligned->image (upscale=1)
            inv[0, 2] -= x0; inv[1, 2] -= y0                          # shift output into bbox-local coords
            sub = cv2.warpAffine(restored[i], inv, (bw, bh), flags=cv2.INTER_LINEAR)
            cov = cv2.warpAffine(ones, inv, (bw, bh), flags=cv2.INTER_NEAREST)  # 1 where the face maps
            cov = cv2.erode(cov, ker, iterations=2)                   # drop the 1-2px dark interpolation fringe
            canvas = f.copy()
            roi = canvas[y0:y1, x0:x1]
            m = cov > 0
            roi[m] = sub[m]                  # paste ONLY where the face covers; uncovered (neck) stays original
            results.append(canvas)           # -> no black border; caller's ellipse mask then blends the mouth
        return results
    # FACE path (bbox=None): keep GFPGAN's soft full-frame paste so the face edges blend into the bg.
    for i, f in enumerate(frames):
        if not ok[i]:
            results.append(f); continue
        fh.clean_all()
        fh.read_image(f)
        fh.affine_matrices = affines[i]
        fh.get_inverse_affine(None)
        fh.add_restored_face(restored[i])
        results.append(fh.paste_faces_to_input_image(upsample_img=None))
    return results


def restore_mouth(inp, out, region="mouth", gfpgan_alpha=1.0, temporal=0.5, margin=1.6, seams=None,
                  precomputed_mouth=None, feather_scale=0.6, sharpen=0.0, single_detect=True):
    """GFPGAN-restore the mouth (or whole face) of video `inp` and write to `out`.

    `seams` is a list of frame indices (loop wraps + scene cuts) where the temporal smooth
    must NOT blend across — at a seam the 3-tap degrades to a 2-/1-tap so it never ghosts
    two unrelated poses together.

    `single_detect=True` (default): run MediaPipe ONCE on the first frame that has a face and reuse
    that mouth ellipse + 5-point landmarks for every frame. This is the big speed win for fixed-camera
    talking-head/avatar clips (the per-frame MediaPipe loop was ~593x on CPU ≈ 4 min). For strongly
    moving heads pass single_detect=False to detect per frame (slower, more accurate GFPGAN alignment).
    """
    seams = set(seams or [])
    restorer = get_restorer()

    # ---- probe basic video props (no full read) -------------------------------------------------
    cap = cv2.VideoCapture(inp)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    n_probe = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    ok0, fr0 = cap.read()
    if not ok0:
        cap.release()
        raise RuntimeError(f"no frames read from {inp}")
    H, W = fr0.shape[:2]
    cap.release()
    print(f"[restore] ~{n_probe} frames {W}x{H} @ {fps:.3f}, region={region} (streaming)")

    # ---- PASS 1: mouth ellipse + 5-point landmarks ----------------------------------------------
    # `landmarks5` is either a full per-frame list (precomputed cache / single_detect=False) or None,
    # in which case `fixed_lm5` (one detection) is reused for every frame by Pass 2.
    t_p1 = time.time()
    landmarks5 = None
    fixed_lm5 = None
    if precomputed_mouth is not None and len(precomputed_mouth[4]) == n_probe:
        cx, cy, hw, hh, landmarks5 = precomputed_mouth
        print(f"[restore] using cached per-frame mouth coords (skipped MediaPipe)")
    elif single_detect:
        # Detect ONCE on the first frame that has a face; reuse its ellipse + landmarks for all frames.
        fd, fm = _make_mediapipe()
        cap = cv2.VideoCapture(inp)
        cx = cy = hw = hh = None
        scanned = 0
        while True:
            ok, f = cap.read()
            if not ok:
                break
            scanned += 1
            center, w, h, lm5 = _detect_mouth_one(fd, fm, f, W, H)
            if center is not None:
                cx, cy = int(center[0]), int(center[1]); hw, hh = int(w), int(h); fixed_lm5 = lm5
                break
        cap.release(); fd.close(); fm.close()
        if cx is None:
            raise RuntimeError("no face found for mouth detection")
        print(f"[restore] single-frame detect: found face on frame {scanned-1}")
    else:
        # Per-frame detection (accurate for moving heads): global-median ellipse + per-frame landmarks.
        if precomputed_mouth is not None:
            print(f"[restore] cached mouth len {len(precomputed_mouth[4])} != ~{n_probe} frames -> detecting")
        fd, fm = _make_mediapipe()
        centers, hws, hhs, landmarks5 = [], [], [], []
        cap = cv2.VideoCapture(inp)
        while True:
            ok, f = cap.read()
            if not ok:
                break
            center, w, h, lm5 = _detect_mouth_one(fd, fm, f, W, H)
            landmarks5.append(lm5)
            if center is not None:
                centers.append(center); hws.append(w); hhs.append(h)
        cap.release()
        fd.close(); fm.close()
        if not centers:
            raise RuntimeError("no face found for mouth detection")
        c = np.array(centers)
        cx, cy = int(np.median(c[:, 0])), int(np.median(c[:, 1]))
        hw, hh = int(np.median(hws)), int(np.median(hhs))
    print(f"[restore] Pass 1 (detect) took {time.time()-t_p1:.2f}s")

    if region == "mouth":
        ax, ay = int(hw*margin), int(hh*margin)
        print(f"[restore] mouth ellipse center=({cx},{cy}) axes=({ax},{ay})")
        m = np.zeros((H, W), np.float32)
        cv2.ellipse(m, (cx, cy), (ax, ay), 0, 0, 360, 1.0, -1)
        k = int(max(ax, ay)*feather_scale) | 1   # feather width ∝ mouth size; lower = tighter viền
        mask = cv2.GaussianBlur(m, (k, k), k*0.35)[..., None]
        # bbox enclosing the FEATHERED ellipse (pad >= the gaussian's reach). The restored face is
        # inverse-warped back into just this box; outside it the mask is ~0 so the original shows through.
        pad = k + 8
        x0, y0 = max(0, cx-ax-pad), max(0, cy-ay-pad)
        x1, y1 = min(W, cx+ax+pad), min(H, cy+ay+pad)
        bbox = (x0, y0, x1, y1)
        print(f"[restore] mouth bbox {x1-x0}x{y1-y0} ({(x1-x0)*(y1-y0)/(W*H)*100:.1f}% of frame)")
    else:
        mask = None
        bbox = None

    def _luma_sharpen(g):
        if sharpen <= 0:
            return g
        ycc = cv2.cvtColor(np.clip(g, 0, 255).astype(np.uint8), cv2.COLOR_BGR2YCrCb).astype(np.float32)
        yb = cv2.GaussianBlur(ycc[..., 0], (0, 0), 3.0)        # sharpen LUMA only -> no màu-môi loang/flicker
        ycc[..., 0] = np.clip(ycc[..., 0] * (1 + sharpen) - yb * sharpen, 0, 255)
        return cv2.cvtColor(ycc.astype(np.uint8), cv2.COLOR_YCrCb2BGR).astype(np.float32)

    # Stream each frame straight into ffmpeg as raw bgr24 (no PNG round-trip).
    cmd = [
        _ffmpeg_bin(), "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}", "-framerate", f"{fps}", "-i", "-",
        "-i", inp, "-map", "0:v", "-map", "1:a?", "-c:v", "libx264", "-crf", "16", "-preset", "medium",
        "-pix_fmt", "yuv420p", "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
        "-c:a", "copy", "-shortest", out,
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    # ---- PASS 2: GFPGAN restore + blend + 3-tap temporal smooth, all STREAMING ------------------
    # We re-read the video in GFPGAN_BATCH batches. The 3-tap temporal smooth needs neighbours, so we
    # keep a rolling window of `blended` (and original frames for the mouth composite) just large
    # enough to emit frame t once frame t+1 exists — bit-identical to the old full-array smooth but
    # holding ~3 frames instead of the whole video.
    mb = mask[bbox[1]:bbox[3], bbox[0]:bbox[2]] if bbox is not None else None
    buf_b = {}   # idx -> blended (bbox-region float for mouth, full float for face)
    buf_f = {}   # idx -> original full frame (mouth path only)
    produced = 0
    written = 0

    def _flush(final):
        nonlocal written
        while written < produced and (written + 1 < produced or final):
            t = written
            # neighbour availability via buffer membership (robust to inaccurate frame counts):
            # during non-final flush t+1 always exists; only the genuine last frame lacks it.
            prev = buf_b[t-1] if (t > 0 and t not in seams) else buf_b[t]
            nxt = buf_b[t+1] if ((t+1) in buf_b and (t+1) not in seams) else buf_b[t]
            sm = (prev + 2*buf_b[t] + nxt) / 4.0
            res = temporal*buf_b[t] + (1-temporal)*sm
            if bbox is not None:
                res = buf_b[t]*(1-mb) + res*mb
                out_f = buf_f[t].copy()
                out_f[bbox[1]:bbox[3], bbox[0]:bbox[2]] = np.clip(res, 0, 255).astype(np.uint8)
                proc.stdin.write(np.ascontiguousarray(out_f).tobytes())
            else:
                proc.stdin.write(np.ascontiguousarray(np.clip(res, 0, 255).astype(np.uint8)).tobytes())
            buf_b.pop(t-1, None); buf_f.pop(t-1, None)   # t-1 no longer needed by any later frame
            written += 1

    t_p2 = time.time()
    cap = cv2.VideoCapture(inp)
    batch, base_idx = [], 0
    eof = False
    while not eof:
        if CANCEL.is_set():
            cap.release(); proc.stdin.close(); proc.wait()
            raise LatentSyncCancelled("cancelled by user")
        ok, f = cap.read()
        if ok:
            batch.append(f)
        else:
            eof = True
        if batch and (len(batch) == GFPGAN_BATCH or eof):
            lms = (landmarks5[base_idx:base_idx + len(batch)] if landmarks5 is not None
                   else [fixed_lm5] * len(batch))
            restored = _gfpgan_restore_batched(restorer, batch, lms, batch_size=GFPGAN_BATCH, bbox=bbox)
            for j, fr in enumerate(batch):
                idx = base_idx + j
                if bbox is not None:
                    ob = fr[bbox[1]:bbox[3], bbox[0]:bbox[2]].astype(np.float32)
                    g = gfpgan_alpha * restored[j][bbox[1]:bbox[3], bbox[0]:bbox[2]].astype(np.float32) + (1 - gfpgan_alpha) * ob
                    g = _luma_sharpen(g)
                    buf_b[idx] = ob * (1 - mb) + g * mb
                    buf_f[idx] = fr
                else:
                    buf_b[idx] = _luma_sharpen(gfpgan_alpha * restored[j].astype(np.float32)
                                               + (1 - gfpgan_alpha) * fr.astype(np.float32))
                produced += 1
            base_idx += len(batch)
            batch = []
            _flush(final=False)
    cap.release()
    _flush(final=True)

    proc.stdin.close()
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"ffmpeg encode failed (exit {rc})")
    dt_p2 = time.time() - t_p2
    dt_p1 = t_p2 - t_p1
    print(f"[restore] Pass 2 (GFPGAN+blend+write) took {dt_p2:.2f}s")
    print(f"[restore] wrote {out} ({written} frames) | TOTAL GFPGAN {dt_p1 + dt_p2:.2f}s (Pass1 {dt_p1:.2f}s + Pass2 {dt_p2:.2f}s)")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", required=True)
    ap.add_argument("--region", choices=["mouth","face"], default="mouth")
    ap.add_argument("--gfpgan_alpha", type=float, default=1.0)
    ap.add_argument("--temporal", type=float, default=0.5)
    ap.add_argument("--margin", type=float, default=1.6)
    ap.add_argument("--feather_scale", type=float, default=0.6)
    ap.add_argument("--sharpen", type=float, default=0.0)
    ap.add_argument("--seams_json", type=str, default=None, help="json file: {\"seams\": [...]}")
    a = ap.parse_args()
    seams = None
    if a.seams_json and os.path.exists(a.seams_json):
        import json
        seams = json.load(open(a.seams_json)).get("seams")
    restore_mouth(a.inp, a.out, region=a.region, gfpgan_alpha=a.gfpgan_alpha,
                  temporal=a.temporal, margin=a.margin, seams=seams,
                  feather_scale=a.feather_scale, sharpen=a.sharpen)


if __name__ == "__main__":
    main()
