"""
Post-process to reduce mouth shimmer and recover edge detail on a LatentSync output.

Root cause (from analysis): the mouth interior is structurally blurred (512-latent/VAE
limit); the perceived flicker is that low-detail band wobbling frame-to-frame. This script
treats the SYMPTOM only, confined to the mouth region with a feathered mask so the rest of
the face is never touched:
  1) locate the mouth robustly with MediaPipe FaceMesh (median over all frames -> stable fixed mask)
  2) light 3-frame temporal blend [1,2,1]/4 inside the mask (reduces shimmer)
  3) mild unsharp inside the mask (recovers some lip/teeth edge)
  4) encode once (crf 16, yuv420p, bt709), copy original audio

Usage:
  python postprocess_mouth.py --in INPUT.mp4 --out OUTPUT.mp4 [--alpha 0.5] [--sharpen 0.6]
    --alpha   : temporal blend weight on ORIGINAL frame inside mask (0=max smoothing, 1=none)
    --sharpen : unsharp amount inside mask (0=off)
"""
import argparse, os, sys, shutil, subprocess
import numpy as np
import cv2

LIP_IDX = sorted(set([
    61,146,91,181,84,17,314,405,321,375,291,308,324,318,402,317,14,87,178,88,95,
    185,40,39,37,0,267,269,270,409,415,310,311,312,13,82,81,80,191,
    # a little jaw/chin margin below the lips (the model regenerates lower face)
    400,377,152,148,176,                       # chin line
]))


def detect_mouth_box(frames):
    import mediapipe as mp
    fm = mp.solutions.face_mesh.FaceMesh(static_image_mode=False, max_num_faces=1,
                                         refine_landmarks=True, min_detection_confidence=0.5)
    H, W = frames[0].shape[:2]
    centers, halfw, halfh = [], [], []
    for i, f in enumerate(frames):
        res = fm.process(cv2.cvtColor(f, cv2.COLOR_BGR2RGB))
        if not res.multi_face_landmarks:
            continue
        lm = res.multi_face_landmarks[0].landmark
        xs = np.array([lm[j].x for j in LIP_IDX]) * W
        ys = np.array([lm[j].y for j in LIP_IDX]) * H
        centers.append(((xs.min()+xs.max())/2, (ys.min()+ys.max())/2))
        halfw.append((xs.max()-xs.min())/2)
        halfh.append((ys.max()-ys.min())/2)
    fm.close()
    if not centers:
        raise RuntimeError("MediaPipe found no face in any frame")
    centers = np.array(centers)
    cx, cy = np.median(centers[:, 0]), np.median(centers[:, 1])
    # axes: median extent + 35% margin so the feather sits outside the regenerated area
    ax = np.median(halfw) * 1.35
    ay = np.median(halfh) * 1.35
    return int(round(cx)), int(round(cy)), int(round(ax)), int(round(ay))


def build_mask(H, W, cx, cy, ax, ay):
    m = np.zeros((H, W), np.float32)
    cv2.ellipse(m, (cx, cy), (ax, ay), 0, 0, 360, 1.0, -1)
    # feather: blur proportional to mask size so the boundary is soft (no seam)
    k = int(max(ax, ay) * 0.6) | 1
    m = cv2.GaussianBlur(m, (k, k), k * 0.35)
    return m[..., None]  # HxWx1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", required=True)
    ap.add_argument("--alpha", type=float, default=0.5)
    ap.add_argument("--sharpen", type=float, default=0.6)
    a = ap.parse_args()

    cap = cv2.VideoCapture(a.inp)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    frames = []
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        frames.append(fr)
    cap.release()
    n = len(frames)
    if n == 0:
        sys.exit("no frames read")
    H, W = frames[0].shape[:2]
    print(f"read {n} frames {W}x{H} @ {fps:.3f}fps")

    cx, cy, ax, ay = detect_mouth_box(frames)
    print(f"mouth ellipse: center=({cx},{cy}) axes=({ax},{ay})")
    mask = build_mask(H, W, cx, cy, ax, ay)

    arr = [f.astype(np.float32) for f in frames]
    out_frames = []
    for t in range(n):
        prev = arr[t - 1] if t > 0 else arr[t]
        nxt = arr[t + 1] if t < n - 1 else arr[t]
        smoothed = (prev + 2 * arr[t] + nxt) / 4.0
        blended = a.alpha * arr[t] + (1 - a.alpha) * smoothed       # temporal smooth
        if a.sharpen > 0:                                          # masked unsharp
            blur = cv2.GaussianBlur(blended, (0, 0), 3.0)
            sharp = cv2.addWeighted(blended, 1 + a.sharpen, blur, -a.sharpen, 0)
            blended = sharp
        out = arr[t] * (1 - mask) + blended * mask                 # confine to mouth
        out_frames.append(np.clip(out, 0, 255).astype(np.uint8))

    tmpdir = a.out + ".frames"
    if os.path.isdir(tmpdir):
        shutil.rmtree(tmpdir)
    os.makedirs(tmpdir)
    for i, f in enumerate(out_frames):
        cv2.imwrite(f"{tmpdir}/{i:05d}.png", f)

    # encode once, copy original audio from the input mp4
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-framerate", f"{fps}", "-i", f"{tmpdir}/%05d.png",
        "-i", a.inp,
        "-map", "0:v", "-map", "1:a?",
        "-c:v", "libx264", "-crf", "16", "-preset", "slow", "-pix_fmt", "yuv420p",
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
        "-c:a", "copy", "-shortest", a.out,
    ]
    subprocess.run(cmd, check=True)
    shutil.rmtree(tmpdir)
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
