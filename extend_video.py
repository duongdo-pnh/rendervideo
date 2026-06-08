"""
Smoothly extend a short input video to >= the audio length for LatentSync, with NO
backwards motion (replaces the pipeline's ping-pong loop).

Strategy (from design workflow): forward-only repeat with a short crossfade at each wrap
seam, wrapping at the interior frame (searched in the last third) most similar to frame 0
so the dissolve bridges the smallest possible pose gap. Also detects hard scene cuts
(concatenated clips) with PySceneDetect. Returns (carrier_path, seam_indices) where
seam_indices are 25fps output-frame positions that downstream temporal smoothing must NOT
blend across (wrap seams + scene cuts).

Why this is safe: LatentSync regenerates the mouth per audio frame and pastes via affine,
so crossfading the base frames never affects audio-mouth sync. Audio is muxed downstream,
so the carrier is written with -an.
"""
import subprocess, json, math, os, shutil
import numpy as np
import cv2

FPS = 25


def _dur(p):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", p])
    return float(json.loads(out)["format"]["duration"])


def _detect_cut_seconds(path, threshold=27.0):
    """Hard scene-cut timestamps (seconds). Empty list if scenedetect unavailable."""
    try:
        from scenedetect import detect, ContentDetector
        scenes = detect(path, ContentDetector(threshold=threshold))
        return [s[0].get_seconds() for s in scenes[1:]]  # start of each scene after the first
    except Exception as e:
        print(f"[extend] scenedetect skipped: {e}")
        return []


def prepare_carrier(vpath, apath, out_path, xfade=6, scene_threshold=27.0):
    """Return (carrier_video_path, seam_indices)."""
    vd, ad = _dur(vpath), _dur(apath)
    cut_idx = [round(t * FPS) for t in _detect_cut_seconds(vpath, scene_threshold)]

    if vd >= ad:
        # Long enough: pipeline truncates to audio length. Seams = internal hard cuts in range.
        need = math.ceil(ad * FPS)
        seams = sorted(i for i in set(cut_idx) if 0 < i < need)
        print(f"[extend] video {vd:.1f}s >= audio {ad:.1f}s: no extension; {len(seams)} scene-cut seam(s)")
        return vpath, seams

    # Shorter: build a forward-loop + crossfade carrier at exactly 25fps.
    norm = out_path + ".norm.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-nostdin", "-i", vpath,
                    "-r", str(FPS), "-an", "-crf", "18", "-pix_fmt", "yuv420p", norm], check=True)
    cap = cv2.VideoCapture(norm)
    frames = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        frames.append(f)
    cap.release()
    N = len(frames)
    if N == 0:
        os.remove(norm)
        print("[extend] could not read frames; falling back to original")
        return vpath, []
    H, W = frames[0].shape[:2]

    # Best forward loop point: interior frame (last third) most similar to frame 0.
    small = [cv2.resize(cv2.cvtColor(f, cv2.COLOR_BGR2GRAY), (64, 64)).astype(np.float32) for f in frames]
    ref = small[0]
    lo = max(int(0.66 * N), N - max(int(2.0 * FPS), xfade + 2))
    hi = N - 1
    if hi - lo >= 2:
        best_k = lo + int(np.argmin([np.mean((small[k] - ref) ** 2) for k in range(lo, hi)]))
    else:
        best_k = N - 1
    seg = frames[:best_k + 1]
    del frames                                         # base frames no longer needed; keep only seg
    S = len(seg)
    X = min(xfade, max(2, S // 4))
    need = math.ceil(ad * FPS) + FPS  # +1s safety so the pipeline never re-enters the loop branch

    # STREAM the forward-loop+crossfade carrier straight into ffmpeg as raw bgr24 instead of
    # materializing all `need` frames (could be hours) + a PNG per frame. The output is bit-identical
    # to the old list-build: each frame position is touched by at most one wrap (X <= S/4 < S/2), so
    # we only buffer the last X frames (the wrap-modifiable tail) and emit everything before it.
    proc = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
         "-s", f"{W}x{H}", "-framerate", str(FPS), "-i", "-",
         "-an", "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", out_path],
        stdin=subprocess.PIPE,
    )
    wrap_seams = []
    emitted = 0
    buf = list(seg)            # invariant: emitted + len(buf) == logical out_frames length
    total = S

    def _emit_keep(keep):
        nonlocal emitted
        k = len(buf) - keep
        i = 0
        while i < k and emitted < need:
            proc.stdin.write(np.ascontiguousarray(buf[i]).tobytes())
            emitted += 1
            i += 1
        del buf[:i]

    _emit_keep(X)                                      # emit all but the wrap-modifiable tail
    while total < need:
        wrap_seams.append(total - X)
        for j in range(X):
            t = (j + 1) / (X + 1)
            buf[-X + j] = cv2.addWeighted(buf[-X + j].astype(np.float32), 1 - t,
                                          seg[j].astype(np.float32), t, 0).astype(np.uint8)
        buf.extend(seg[X:])
        total += S - X
        _emit_keep(X)
    for f in buf:                                      # drain remaining tail up to `need`
        if emitted >= need:
            break
        proc.stdin.write(np.ascontiguousarray(f).tobytes())
        emitted += 1
    proc.stdin.close()
    rc = proc.wait()
    os.remove(norm)
    if rc != 0:
        raise RuntimeError(f"[extend] ffmpeg encode failed (exit {rc})")

    # Internal hard cuts recur every loop period -> detect on the FINAL video (dissolves at
    # wrap seams are gradual so scenedetect won't flag them; we add wrap seams explicitly).
    cut_final = [round(t * FPS) for t in _detect_cut_seconds(out_path, scene_threshold)]
    n = emitted
    seams = sorted({w for w in wrap_seams if 0 < w < n} | {c for c in cut_final if 0 < c < n})
    print(f"[extend] {vd:.1f}s -> {n/FPS:.1f}s ({n}f) | loop@{best_k} xfade={X} | "
          f"{len(wrap_seams)} wrap + {len(cut_final)} cut = {len(seams)} seam(s)")
    return out_path, seams


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    path, seams = prepare_carrier(a.video, a.audio, a.out)
    print("carrier:", path)
    print("seams:", seams)
