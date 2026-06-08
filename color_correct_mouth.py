"""
GFPGAN-free mouth post-process: color-correct the rendered mouth toward the original
(carrier) frame's lower-face skin via a per-channel monotone LUT (histogram matching on a
skin RING, applied to the mouth interior), then a 15px gaussian-feather blend.

Fixes color/tone drift + paste seam WITHOUT GFPGAN's real-face prior (no magenta lips, no
identity shift). It changes COLOR only — it does NOT add sharpness (the 512/VAE softness
remains; use GFPGAN if you need crisper teeth and accept the color/identity tradeoff).

  python color_correct_mouth.py --in RENDERED.mp4 --carrier CARRIER.mp4 --out OUT.mp4 \
      [--feather 15] [--curve_smooth 0.6] [--sharpen 0.0] [--seams_json s.json]
Importable: color_correct_mouth(rendered, carrier, out, seams=[...])
"""
import argparse, os, shutil, subprocess
import numpy as np
import cv2

LIP_IDX = sorted(set([
    61, 146, 91, 181, 84, 17, 314, 405, 321, 375, 291, 308, 324, 318, 402, 317, 14, 87, 178, 88, 95,
    185, 40, 39, 37, 0, 267, 269, 270, 409, 415, 310, 311, 312, 13, 82, 81, 80, 191,
    400, 377, 152, 148, 176,
]))


def _read(path):
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    fr = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        fr.append(f)
    cap.release()
    if not fr:
        raise RuntimeError(f"no frames from {path}")
    return fr, fps


def _make_mediapipe():
    import mediapipe as mp
    fd = mp.solutions.face_detection.FaceDetection(model_selection=1, min_detection_confidence=0.3)
    fm = mp.solutions.face_mesh.FaceMesh(static_image_mode=False, max_num_faces=1,
                                         refine_landmarks=True, min_detection_confidence=0.5)
    return fd, fm


def _ellipse_one(fd, fm, f, W, H):
    """One frame -> (center|None, hw|None, hh|None). full-range FaceDetection -> crop+zoom -> FaceMesh,
    so it works on SMALL faces (wide product-sales shots) where plain FaceMesh misses the face."""
    rgb = cv2.cvtColor(f, cv2.COLOR_BGR2RGB)
    det = fd.process(rgb)
    if not det.detections:
        return None, None, None
    bb = det.detections[0].location_data.relative_bounding_box
    bx, by, bw, bh = bb.xmin*W, bb.ymin*H, bb.width*W, bb.height*H
    pad = bh * 0.5
    x0, y0 = max(0, int(bx-pad)), max(0, int(by-pad))
    x1, y1 = min(W, int(bx+bw+pad)), min(H, int(by+bh+pad))
    cw, ch = x1-x0, y1-y0
    if cw < 8 or ch < 8:
        return None, None, None
    r = fm.process(rgb[y0:y1, x0:x1])
    if not r.multi_face_landmarks:
        return None, None, None
    lm = r.multi_face_landmarks[0].landmark
    xs = np.array([x0 + lm[j].x*cw for j in LIP_IDX])
    ys = np.array([y0 + lm[j].y*ch for j in LIP_IDX])
    return (((xs.min()+xs.max())/2, (ys.min()+ys.max())/2),
            (xs.max()-xs.min())/2, (ys.max()-ys.min())/2)


def _detect_ellipse(frames, W, H):
    fd, fm = _make_mediapipe()
    c, hw, hh = [], [], []
    for f in frames:
        center, w, h = _ellipse_one(fd, fm, f, W, H)
        if center is not None:
            c.append(center); hw.append(w); hh.append(h)
    fd.close(); fm.close()
    if not c:
        raise RuntimeError("no face for mouth detection")
    c = np.array(c)
    return (int(np.median(c[:, 0])), int(np.median(c[:, 1])),
            int(np.median(hw)), int(np.median(hh)))


def _lut_from_ring(rendered, carrier, ring_bool):
    """Per-channel monotone LUT mapping rendered-skin CDF -> carrier-skin CDF (match_histograms
    restricted to the skin ring). Applied to the mouth so its histogram is SHIFTED, not flattened."""
    ramp = np.arange(256)
    luts = []
    for ch in range(3):
        rv = rendered[..., ch][ring_bool]
        cv_ = carrier[..., ch][ring_bool]
        rh = np.bincount(rv.astype(np.int32), minlength=256).astype(np.float64)
        chh = np.bincount(cv_.astype(np.int32), minlength=256).astype(np.float64)
        rc = np.cumsum(rh); rc /= max(rc[-1], 1e-6)
        cc = np.cumsum(chh); cc /= max(cc[-1], 1e-6)
        lut = np.interp(rc, cc, ramp)
        luts.append(np.clip(np.round(lut), 0, 255).astype(np.uint8))
    return luts


def color_correct_mouth(rendered, carrier, out, feather=25, curve_smooth=0.6,
                        sharpen=0.5, temporal=0.5, seams=None):
    seams = set(seams or [])

    # ---- probe (no full read). rendered = lip-synced carrier (often truncated to audio); align by
    # index, trim to min. STREAMING: 3 light passes over the videos instead of all frames in RAM. ----
    rcap = cv2.VideoCapture(rendered)
    fps = rcap.get(cv2.CAP_PROP_FPS) or 25.0
    nr = int(rcap.get(cv2.CAP_PROP_FRAME_COUNT))
    ok0, fr0 = rcap.read()
    if not ok0:
        rcap.release(); raise RuntimeError(f"no frames from {rendered}")
    H, W = fr0.shape[:2]
    rcap.release()
    ccap = cv2.VideoCapture(carrier)
    nc = int(ccap.get(cv2.CAP_PROP_FRAME_COUNT))
    ccap.release()
    n = min(nr, nc)

    # ---- PASS A: global mouth ellipse (median over rendered frames), streaming -------------------
    fd, fm = _make_mediapipe()
    centers, hws, hhs = [], [], []
    rcap = cv2.VideoCapture(rendered); idx = 0
    while idx < n:
        ok, f = rcap.read()
        if not ok:
            break
        center, w, h = _ellipse_one(fd, fm, f, W, H)
        if center is not None:
            centers.append(center); hws.append(w); hhs.append(h)
        idx += 1
    rcap.release(); fd.close(); fm.close()
    if not centers:
        raise RuntimeError("no face for mouth detection")
    c = np.array(centers)
    cx, cy = int(np.median(c[:, 0])), int(np.median(c[:, 1]))
    hw, hh = int(np.median(hws)), int(np.median(hhs))
    ax, ay = int(hw * 1.35), int(hh * 1.35)

    m = np.zeros((H, W), np.float32)
    cv2.ellipse(m, (cx, cy), (ax, ay), 0, 0, 360, 1.0, -1)
    k = int(feather) | 1
    apply_mask = cv2.GaussianBlur(m, (k, k), feather * 0.5)[..., None]

    outer = np.zeros((H, W), np.uint8); cv2.ellipse(outer, (cx, cy), (int(ax * 1.8), int(ay * 1.8)), 0, 0, 360, 1, -1)
    inner = np.zeros((H, W), np.uint8); cv2.ellipse(inner, (cx, cy), (int(ax * 1.25), int(ay * 1.25)), 0, 0, 360, 1, -1)
    ring = (outer > 0) & (inner == 0)

    # ---- PASS B: GLOBAL LUT (one curve over the whole video so mouth color never jumps frame-to-
    # frame). Aggregate ring histograms across SAMPLED frames (rendered+carrier paired), streaming. --
    step = max(1, n // 120)
    accR = [np.zeros(256, np.float64) for _ in range(3)]
    accC = [np.zeros(256, np.float64) for _ in range(3)]
    rcap = cv2.VideoCapture(rendered); ccap = cv2.VideoCapture(carrier)
    for t in range(n):
        okr, rfr = rcap.read(); okc, cfr = ccap.read()
        if not (okr and okc):
            break
        if t % step != 0:
            continue
        if cfr.shape[:2] != (H, W):
            cfr = cv2.resize(cfr, (W, H))
        for ch in range(3):
            accR[ch] += np.bincount(rfr[..., ch][ring].astype(np.int32), minlength=256)
            accC[ch] += np.bincount(cfr[..., ch][ring].astype(np.int32), minlength=256)
    rcap.release(); ccap.release()
    ramp = np.arange(256)
    global_luts = []
    for ch in range(3):
        rc = np.cumsum(accR[ch]); rc /= max(rc[-1], 1e-6)
        cc = np.cumsum(accC[ch]); cc /= max(cc[-1], 1e-6)
        global_luts.append(np.clip(np.round(np.interp(rc, cc, ramp)), 0, 255).astype(np.uint8))

    # ---- PASS C: apply LUT + 3-tap temporal smooth + feathered composite, STREAMING to ffmpeg ----
    # corr = LUT(rendered); temporal smooth on corr (needs neighbours -> rolling window of 3); then
    # composite rendered*(1-mask)+smoothed*mask. Carrier is not needed here. Bit-identical to the old
    # full-array path but holding ~3 frames instead of the whole video.
    proc = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
         "-s", f"{W}x{H}", "-framerate", f"{fps}", "-i", "-",
         "-i", rendered, "-map", "0:v", "-map", "1:a?", "-c:v", "libx264", "-crf", "16",
         "-preset", "slow", "-pix_fmt", "yuv420p", "-color_primaries", "bt709",
         "-color_trc", "bt709", "-colorspace", "bt709", "-c:a", "copy", "-shortest", out],
        stdin=subprocess.PIPE,
    )
    buf_corr, buf_rf = {}, {}
    produced = 0
    written = 0

    def _flush(final):
        nonlocal written
        while written < produced and (written + 1 < produced or final):
            t = written
            cur = buf_corr[t].astype(np.float32)
            if temporal < 1.0:
                prev = buf_corr[t-1].astype(np.float32) if (t > 0 and t not in seams) else cur
                nxt = buf_corr[t+1].astype(np.float32) if ((t+1) in buf_corr and (t+1) not in seams) else cur
                sm = (prev + 2*cur + nxt) / 4.0
                cur = temporal*cur + (1-temporal)*sm
            if sharpen > 0:
                blur = cv2.GaussianBlur(cur, (0, 0), 3.0)
                cur = cv2.addWeighted(cur, 1 + sharpen, blur, -sharpen, 0)
            outf = buf_rf[t].astype(np.float32) * (1 - apply_mask) + cur * apply_mask
            proc.stdin.write(np.ascontiguousarray(np.clip(outf, 0, 255).astype(np.uint8)).tobytes())
            buf_corr.pop(t-1, None); buf_rf.pop(t-1, None)
            written += 1

    rcap = cv2.VideoCapture(rendered); idx = 0
    while idx < n:
        ok, f = rcap.read()
        if not ok:
            break
        corr = f.copy()
        for ch in range(3):
            corr[..., ch] = cv2.LUT(f[..., ch], global_luts[ch])
        buf_corr[idx] = corr
        buf_rf[idx] = f
        produced += 1
        idx += 1
        _flush(final=False)
    rcap.release()
    _flush(final=True)

    proc.stdin.close()
    rc = proc.wait()
    if rc != 0:
        raise RuntimeError(f"[color_correct] ffmpeg encode failed (exit {rc})")
    print(f"[color_correct] wrote {out} (n={written}, mouth=({cx},{cy}) ax={ax} ay={ay}, {len(seams)} seam)")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--carrier", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--feather", type=int, default=25)
    ap.add_argument("--curve_smooth", type=float, default=0.6)
    ap.add_argument("--sharpen", type=float, default=0.5)
    ap.add_argument("--temporal", type=float, default=0.5)
    ap.add_argument("--seams_json", default=None)
    a = ap.parse_args()
    seams = None
    if a.seams_json and os.path.exists(a.seams_json):
        import json
        seams = json.load(open(a.seams_json)).get("seams")
    color_correct_mouth(a.inp, a.carrier, a.out, feather=a.feather, curve_smooth=a.curve_smooth,
                        sharpen=a.sharpen, temporal=a.temporal, seams=seams)


if __name__ == "__main__":
    main()
