"""
Driving-pose video for Wan2.2-Fun-5B-Control: DWPose (yolox_l + dw-ll_ucoco_384, the model's training annotator) on
the GPU through rtmlib/onnxruntime, aligned to the person in the reference image and drawn with VideoX-Fun's
OpenPose renderer (body, hands, face), so the control frames look like the ones the model was trained on.
"""

import hashlib
import importlib.metadata
import importlib.util
import inspect
import itertools
import os
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor

import numpy as np

from fun5b import config

FUN_ROOT = config.VIDEOX_DIR
DWPOSE_DIR = os.path.join(config.CKPT_DIR, "wan22-fun-5b/DWPose")
POSE_CACHE_DIR = config.POSE_CACHE_DIR
sys.path.insert(0, os.path.join(FUN_ROOT, "comfyui"))

from annotator.dwpose_utils import draw_pose  # noqa: E402
from annotator.dwpose_utils.wholebody import Wholebody  # noqa: E402

MIN_SCORE = 0.3  # VideoX-Fun drops keypoints below this when drawing
SMOOTH_SIGMA = 2.0  # frames (at 30fps): removes the per-frame shake, keeps motion faster than ~0.2s


class PoseDetector:
    def __init__(self):
        from rtmlib import Wholebody as RtmWholebody

        self.model = RtmWholebody(
            det=os.path.join(DWPOSE_DIR, "yolox_l.onnx"), det_input_size=(640, 640),
            pose=os.path.join(DWPOSE_DIR, "dw-ll_ucoco_384.onnx"), pose_input_size=(288, 384),
            to_openpose=True, backend="onnxruntime", device="cuda",
        )

    def __call__(self, rgb):
        """(134, 3) keypoints (x, y, score in pixels, OpenPose order with neck) of the biggest person, or None.
        Biggest, not most visible: a narrow shape on a shop shelf was detected with one visible joint more than the
        presenter in every other frame, the skeleton jumped between them and the model walked the person off-frame."""
        keypoints, scores = self.model(rgb[..., ::-1].copy())  # rtmlib expects BGR
        if len(keypoints) == 0:
            return None
        visible = scores[:, :18] > MIN_SCORE

        def area(i):
            body = keypoints[i, :18][visible[i]]
            return np.ptp(body[:, 0]) * np.ptp(body[:, 1]) if len(body) >= 4 else -1.0

        best = max(range(len(keypoints)), key=area)
        if area(best) < 0:
            best = int(np.argmax(visible.sum(axis=1)))
        return np.concatenate([keypoints[best], scores[best][:, None]], axis=1)


HEAD = np.r_[[0, 14, 15, 16, 17], 24:92]  # nose, eyes, ears, 68 face points


def _face_size(kp):
    """Diagonal of the visible face points' box (0 if none)."""
    face = kp[24:92][kp[24:92, 2] > MIN_SCORE, :2]
    return float(np.linalg.norm(face.max(0) - face.min(0))) if len(face) > 4 else 0.0


def _torso(kp):
    """Neck position and a body scale (neck -> mid-hip, else shoulder width × 2.2)."""
    neck = kp[1, :2]
    if kp[8, 2] > MIN_SCORE and kp[11, 2] > MIN_SCORE:
        return neck, np.linalg.norm((kp[8, :2] + kp[11, :2]) / 2 - neck)
    return neck, np.linalg.norm(kp[2, :2] - kp[5, :2]) * 2.2


def smooth_keypoints(kps, sigma=SMOOTH_SIGMA):
    """
    Zero-phase temporal smoothing of (T, 134, 3) keypoints: a Gaussian over time weighted by each point's score, so
    low-confidence frames borrow from their neighbours. DWPose shakes about as much from frame to frame as the real
    motion moves (hands ~9px), and the model renders that shake as a jittering body.
    """
    t = np.arange(len(kps))
    radius = int(3 * sigma)
    out = kps.copy()
    w_conf = np.clip(kps[..., 2], 0, 1)  # (T, K)
    for i in range(len(kps)):
        lo, hi = max(0, i - radius), min(len(kps), i + radius + 1)
        g = np.exp(-0.5 * ((t[lo:hi] - i) / sigma) ** 2)[:, None]  # (W, 1)
        w = g * w_conf[lo:hi]  # (W, K)
        total = w.sum(axis=0)
        ok = total > 1e-6
        out[i, ok, :2] = (w[:, ok, None] * kps[lo:hi, ok, :2]).sum(axis=0) / total[ok, None]
        out[i, :, 2] = (g * w_conf[lo:hi]).sum(axis=0) / g.sum()  # a point seen by the neighbours stays drawn
    return out


def pose_frames(detector, reference_rgb, detections, lead_in=0, settle=0):
    """
    Pose keypoints ((134, 3) arrays, reference coordinates), one per driving frame; draw_pose_frame() turns one into
    the OpenPose canvas the model takes. `detections`: the driving video's detect_frames() (a DrivingPoses entry),
    which does not depend on the reference, so it is computed once per driving video. The driving skeleton is
    scaled by torso length and its neck anchored on the reference's, from the first frame with a visible neck (one
    transform for the whole video, so the motion itself is unchanged); frames without a detection reuse the previous
    one, and the keypoints are smoothed over time.
    lead_in > 0 prepends the reference's own pose and `lead_in` poses moving from it to the first driving pose, so a
    render can start from the reference image itself (those 1 + lead_in frames are dropped afterwards). settle > 0
    then holds the first driving pose for `settle` more frames (dropped too): the model takes ~12-14 frames to turn
    the reference's face into its own, and the first written frames came out mid-way (eyes half shut, ragged teeth).
    """
    reference = detector(reference_rgb)
    if reference is None:
        raise ValueError("Không tìm thấy người trong ảnh nhân vật.")
    first = next((d for d in detections if d is not None and d[1, 2] > MIN_SCORE), None)
    if first is None:
        raise ValueError("Không tìm thấy người trong video chuyển động.")
    (ref_neck, ref_scale), (drv_neck, drv_scale) = _torso(reference), _torso(first)
    scale = ref_scale / max(drv_scale, 1e-6)
    filled, last = [], first
    for kp in detections:  # frames without a detection reuse the previous one
        last = kp if kp is not None else last
        filled.append(last)
    aligned = smooth_keypoints(np.stack(filled))
    aligned[..., :2] = (aligned[..., :2] - drv_neck) * scale + ref_neck
    # Heads are not proportional to torsos from person to person: a face drawn bigger than the reference's makes the
    # model redraw a different, bigger face. Scale the head points (nose, eyes, ears, face) about the nose to match.
    sizes = [_face_size(kp) for kp in aligned]
    sizes = [x for x in sizes if x > 0]
    if sizes and _face_size(reference) > 0:
        head_scale = _face_size(reference) / float(np.median(sizes))
        for kp in aligned:
            kp[HEAD, :2] = kp[0, :2] + (kp[HEAD, :2] - kp[0, :2]) * head_scale
    _keep_reference_face(aligned, reference)
    lead = []
    if lead_in:
        start, end = reference, aligned[0]
        both = (start[:, 2] > MIN_SCORE) & (end[:, 2] > MIN_SCORE)
        lead.append(start)
        for k in range(1, lead_in + 1):  # straight-line move of every joint seen in both poses
            t = k / (lead_in + 1)
            kp = end.copy()
            kp[both, :2] = (1 - t) * start[both, :2] + t * end[both, :2]
            lead.append(kp)
        lead += [aligned[0].copy() for _ in range(settle)]
    return [*lead, *aligned]


NOSE_TIP = 24 + 30  # the face's 68 points start at 24 (OpenPose order)


def _keep_reference_face(aligned, reference):
    """The driver's face shape (jaw, brows, eye spacing, lips) is drawn into the control skeleton and the model copies
    it: a reference came out with the driver's slimmer jaw and wider eyes. Keep the reference's own face shape and
    take only the driver's movement of it (each point's offset from its median position over the video, about the nose
    tip). Frames where the face is not fully seen keep the driver's points."""
    face = np.r_[24:92]
    if (reference[face, 2] > MIN_SCORE).sum() < 60:
        return
    seen = (aligned[:, face, 2] > MIN_SCORE).all(axis=1)
    if seen.sum() < 5:
        return
    nose = aligned[:, NOSE_TIP : NOSE_TIP + 1, :2]
    offset = aligned[:, face, :2] - nose
    shape = reference[face, :2] - reference[NOSE_TIP, :2]
    moved = nose + shape + (offset - np.median(offset[seen], axis=0))
    for t in np.flatnonzero(seen):
        aligned[t, face, :2] = moved[t]


def detect_frames(detector, frames, threads=4, batch=64):
    """detector() over `frames` (any iterable, e.g. a generator, so a long video never sits in memory as images),
    `threads` frames at a time, `batch` frames held at once. Most of a DWPose call is CPU pre/post-processing around
    the ONNX runs: 4 threads took 93 -> 42 ms per frame with identical keypoints."""
    frames, out = iter(frames), []
    with ThreadPoolExecutor(threads) as pool:
        while chunk := list(itertools.islice(frames, batch)):
            out.extend(pool.map(detector, chunk))
    return out


class DrivingPoses:
    """
    detect_frames() of driving videos, computed once per video and reused by every render of it. The detections do
    not depend on the reference (pose_frames() aligns them to it afterwards in 0.3s), and they were the ~65s "pose"
    step of every render (30s of 1080×1920 60fps): now the UI starts them as soon as a video is uploaded and Generate
    finds them done, and another render of the same motion (other reference, preset or seed) skips them. Keyed by a hash
    of the file's bytes (an upload lands at a new temporary path each time) plus `spec`, everything else the frames
    depend on; kept in memory and in `cache_dir` (~3MB per 30s, the newest MAX_FILES), so a restart keeps them.
    The detections are those of the uncached path, bit for bit (the detector is deterministic): the cache stores
    float64 keypoints as they are and hands them out read-only.
    """

    MAX_MEMORY = 16  # videos kept in memory
    MAX_FILES = 200  # videos kept on disk

    def __init__(self, detector, read_frames, spec, cache_dir=POSE_CACHE_DIR):
        """`read_frames(video_path)`: the driving frames as the renderer sees them (run.read_driving); `spec`: a
        string naming everything they depend on besides the file (frame rate, size, resampling)."""
        self.detector, self.read_frames, self.cache_dir = detector, read_frames, cache_dir
        models = [getattr(m, "onnx_model", "") for m in (detector.model.det_model, detector.model.pose_model)]
        models = [f"{os.path.basename(m)}:{os.path.getsize(m)}" if os.path.isfile(m) else str(m) for m in models]
        code = "".join(inspect.getsource(f) for f in (PoseDetector, detect_frames, read_frames))
        libs = [importlib.metadata.version(m) for m in ("rtmlib", "onnxruntime-gpu", "av", "decord")
                if importlib.util.find_spec(m.replace("-gpu", ""))]
        self.spec = f"v2|{spec}|{'|'.join(models)}|{MIN_SCORE}|{'|'.join(libs)}|{hashlib.sha256(code.encode()).hexdigest()}"
        self._jobs = {}  # key -> Future (insertion order: oldest first)
        self._lock = threading.Lock()
        self._background = threading.Semaphore(1)  # one prefetch at a time: they share the GPU with a running render

    def prefetch(self, video_path):
        """Start detecting `video_path` in a background thread, unless it is done or running; returns at once."""
        self._job(video_path, background=True)

    def __call__(self, video_path):
        """Detections of `video_path` (one (134, 3) array or None per driving frame): from the cache, from a running
        prefetch (waited for), or computed now in this thread."""
        return self._job(video_path, background=False).result()

    def _job(self, video_path, background):
        key = self._key(video_path)
        with self._lock:
            job = self._jobs.get(key)
            new = job is None or (job.done() and job.exception() is not None)  # a failed one is retried
            if new:
                job = self._jobs[key] = Future()
                job.claimed = False  # set by the thread that runs it
                done = [k for k, j in self._jobs.items() if j.done()]
                for k in done[: max(0, len(self._jobs) - self.MAX_MEMORY)]:
                    del self._jobs[k]
            mine = not background and not job.claimed  # a render does not queue behind other prefetches
            job.claimed |= mine
        if mine:
            self._run(job, video_path, key)
        elif background and new:
            threading.Thread(target=self._prefetch, args=(job, video_path, key), name="pose-prefetch",
                             daemon=True).start()
        return job

    def _prefetch(self, job, video_path, key):
        with self._background:
            with self._lock:
                if job.claimed:  # a render needed it first and ran it itself
                    return
                job.claimed = True
            self._run(job, video_path, key)

    def _run(self, job, video_path, key):
        try:
            job.set_result(self._load_or_detect(video_path, key))
        except BaseException as e:  # noqa: BLE001 - handed to whoever waits for the job
            job.set_exception(e)

    def _key(self, video_path):
        digest = hashlib.sha256(self.spec.encode())
        with open(video_path, "rb") as f:
            while block := f.read(1 << 22):
                digest.update(block)
        return digest.hexdigest()[:40]

    def _load_or_detect(self, video_path, key):
        path = os.path.join(self.cache_dir, key + ".npz")
        cached = self._load(path)
        if cached is not None:
            keypoints, found = cached
        else:
            detections = detect_frames(self.detector, self.read_frames(video_path))
            found = np.array([d is not None for d in detections], bool)
            blank = next((np.zeros_like(d) for d in detections if d is not None), np.zeros((134, 3)))
            keypoints = np.stack([d if d is not None else blank for d in detections] or [blank])[: len(detections)]
            self._save(path, keypoints, found)
        keypoints.setflags(write=False)  # shared by every render of this video
        return [kp if ok else None for kp, ok in zip(keypoints, found)]

    @staticmethod
    def _load(path):
        try:
            with np.load(path) as data:
                keypoints, found = data["keypoints"], data["found"]
        except Exception:  # noqa: BLE001 - missing, partial or from an older layout: detect again
            return None
        try:
            os.utime(path)  # recently used ones survive pruning
        except OSError:
            pass
        return keypoints, found

    def _save(self, path, keypoints, found):
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "wb") as f:
                np.savez(f, keypoints=keypoints, found=found)
            os.replace(tmp, path)
            files = sorted((e for e in os.scandir(self.cache_dir) if e.name.endswith(".npz")),
                           key=lambda e: e.stat().st_mtime, reverse=True)
            for e in files[self.MAX_FILES :]:
                os.remove(e.path)
        except OSError as e:  # a cache that cannot be written only costs the next render the detection again
            print(f"pose cache not written: {e}", flush=True)


def draw_pose_frame(kp, height, width):
    """OpenPose canvas (RGB, height×width) of one pose_frames() entry."""
    return draw_pose(Wholebody.format_result(kp[None]), height, width)
