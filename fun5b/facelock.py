"""
Face-anchored colour lock: holds the person's skin to the reference's across the video. Fun-5B relights the person on
its own, also within a chunk and with a flat driving video: the face rose 156 -> 178 L within ~10 frames (a visible
flash), and the person colour lock in matting.py, smoothed over ~1s, was far too slow to hold it.

Per finished frame the face skin is measured (DWPose's 68 face points, inter-quartile mean of L so eyes, brows, lips
and highlights do not count). Unreliable measures are dropped: no or small face, a hand over it, single-frame
outliers. The correction towards the reference face (L as a gain, a/b as shifts) is smoothed over time with a
centred Gaussian: a chunk is composited all at once, so its later frames are known; the window stops at the frames
generated so far. Measured on a 30s clip: face L std 6.4 -> 1.2, the flash +22 -> +5, mean 174.9 -> 177.7 against
the reference's 178.7.

The face's correction holds the head and the skin; the clothes get their own. The person outside the head is split
into the colour groups of the reference (k-means, near ones merged; the pharmacy scrubs: blue, skin, hair) and every
group that is not skin is locked to its colour in the reference the same way as the face. One face-derived
correction for the whole person made the clothes flicker (30s pharmacy clip, 888 frames): the model relights face
and clothes differently (at two chunk starts it drew the face +5 L brighter and the scrubs -3..-5 L darker), so
holding the face put a -7 L step on the scrubs at those seams, and the fast face correction, which also follows
expression (she laughs with her eyes closed: the face reads +9 L, the whole body was darkened 4.4%), turned the
model's own 1-4 L swells on the scrubs into 4-10 L ones. With their own lock the scrubs' largest seam step fell
7.1 -> 1.4 L, their 0.2-1.5s swells (std) 1.71 -> 0.66 L, their error to the reference 2.4 -> 0.4 dE00; the face
stayed as held as before. One correction blended from face, clothes and hands made the face flash +6..+9 L every
chunk instead. Arms, hands and neck keep the face's correction: a lock of their own (measured on arms and neck)
followed how the pose shades them and put 8-16 L steps on the hands.

Chunk seams: a chunk is given the last finished frames (colour-locked) and starts from their colours, then relaxes to
its own look within ~10 frames. Every lock restarts its smoothing window at the seam instead of averaging the two
chunks' raw colours, and fits a local line, so the correction follows the new chunk's frames from its first one and
the output stays at the reference's colours on both sides (face: 1s mean range 2.2 -> 0.7 L).
"""

import cv2
import numpy as np

FACE = np.r_[24:92]  # DWPose (OpenPose order) face points
HANDS = np.r_[92:134]
MIN_SCORE = 0.3
MIN_PIXELS = 3000  # a face smaller than this (in the 640x1152 render) is not measured
SIGMA = 4.0  # frames, temporal smoothing of the correction (also the clothes': 8 left 1.3x their seam steps)
GAIN = (0.8, 1.25)
SHIFT = 10.0
SCALE = 2  # the body is measured at half resolution
HEAD_SCALE = 4  # and the soft head map made at a quarter (11.5 -> ~2 ms a frame)
GROUPS = 5  # colour groups of the reference person outside the head, before merging
L_WEIGHT = 0.25  # groups are told apart by colour more than by brightness (OpenCV Lab, L x this)
GROUP_MERGE = 24.0  # groups closer than this (in that space) are one
GROUP_SPREAD = 6.0  # softness of a pixel's membership in the groups when the correction is applied (same space)
GROUP_MIN = 0.03  # a group with less of the reference person than this is dropped
SKIN_AB = 15.0  # a group this close to the reference face in a/b is skin (or hair): it takes the face's correction
BODY_GAIN = 0.10  # a group's L gain stays within +-10% of the face's
BODY_SHIFT = 5.0  # and its a/b within +-5 of the face's


def face_colour(rgb, detector=None, kp=None):
    """Robust Lab mean (3,) of the face skin in `rgb`, or None when it cannot be trusted. The face is found with
    `detector`, or taken from the keypoints `kp` the frame was rendered from."""
    if kp is None:
        kp = detector(rgb)
    if kp is None:
        return None
    face = kp[FACE][kp[FACE, 2] > MIN_SCORE, :2]
    if len(face) < 40:
        return None
    hull = cv2.convexHull(face.astype(np.int32))
    mask = cv2.erode(cv2.fillPoly(np.zeros(rgb.shape[:2], np.uint8), [hull], 1), np.ones((7, 7), np.uint8))
    if mask.sum() < MIN_PIXELS:
        return None
    hands = kp[HANDS][kp[HANDS, 2] > MIN_SCORE, :2]
    if any(cv2.pointPolygonTest(hull, (float(x), float(y)), False) >= 0 for x, y in hands):
        return None  # a hand over the face
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)[mask.astype(bool)].astype(np.float32)
    lo, hi = np.percentile(lab[:, 0], [20, 80])
    skin = lab[(lab[:, 0] >= lo) & (lab[:, 0] <= hi)]
    return skin.mean(0) if len(skin) > 50 else lab.mean(0)


def head_map(shape, kp, scale=HEAD_SCALE):
    """Soft map (float32, 1 on the head) of a frame of `shape` (H, W) at 1/`scale` of its size, from the face keypoints
    `kp`: the hull grown ~30px (hairline, ears, jaw) and feathered over ~12px, so the face's correction fades into the
    body's over the neck. Zero without a face."""
    m = np.zeros((shape[0] // scale, shape[1] // scale), np.float32)
    pts = None if kp is None else kp[FACE][kp[FACE, 2] > MIN_SCORE, :2]
    if pts is not None and len(pts) >= 20:
        cv2.fillPoly(m, [cv2.convexHull((pts / scale).astype(np.int32))], 1.0)
        grow = int(61 / scale) | 1
        m = cv2.dilate(m, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (grow, grow)))
        m = cv2.GaussianBlur(m, (0, 0), 12 / scale)
    return m


def _hands_mask(shape, kp, scale):
    """Bool mask (at 1/scale) around the visible hands: they turn in and out of the light with the pose."""
    m = np.zeros((shape[0] // scale, shape[1] // scale), np.uint8)
    if kp is not None:
        for hand in (kp[92:113], kp[113:134]):
            p = hand[hand[:, 2] > MIN_SCORE, :2]
            if len(p) >= 8:
                cv2.fillPoly(m, [cv2.convexHull((p / scale).astype(np.int32))], 1)
    return cv2.dilate(m, np.ones((25 // scale | 1,) * 2, np.uint8)).astype(bool)


def _features(lab):
    """OpenCV Lab -> the space colour groups are told apart in. Brightness counts less than colour, so a group keeps
    its pixels when the model shades it darker: with L at full weight the scrubs split into a light and a mid blue
    group, a darker frame moved pixels from one to the other, each group's mean hardly changed and the lock did not
    see the change (seam step 1.9 L left, against 1.4)."""
    return np.stack([lab[..., 0] * L_WEIGHT, lab[..., 1], lab[..., 2]], -1)


class FaceLock:
    def __init__(self, reference, detector, alpha=None):
        """`alpha` (HxW, the reference person's matte; Matting.plate() keeps it) enables the clothes' locks; without it
        the face's correction is applied to the whole person."""
        self.detector = detector
        kp = detector(reference)
        self.target = None if kp is None else face_colour(reference, kp=kp)
        self.centres = self.group_targets = None
        if self.target is not None and alpha is not None:
            self._groups(reference, alpha, kp)
        # skin-coloured groups (arms, hands, neck; hair, whose a/b is close) take the face's correction
        self.skin = [] if self.centres is None else [bool(np.hypot(*(t[1:] - self.target[1:])) < SKIN_AB)
                                                     for t in self.group_targets]
        self.tracks = [_Track() for _ in range(1 + len(self.skin))]

    def _groups(self, reference, alpha, kp):
        """Colour groups of the reference person outside the head (k-means, the closest merged while nearer than
        GROUP_MERGE) and each group's colour there, its target."""
        lab, person = _body(reference, alpha, kp)
        px = _features(lab[person]).astype(np.float32)
        if len(px) < 2000:
            return
        cv2.setRNGSeed(0)  # the same groups for the same reference, render after render
        _, label, centres = cv2.kmeans(px, GROUPS, None, (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 50, 0.5),
                                       5, cv2.KMEANS_PP_CENTERS)
        count = list(np.bincount(label.ravel(), minlength=GROUPS).astype(np.float64))
        centres = list(centres.astype(np.float64))
        while len(centres) > 1:
            dist, i, j = min((np.linalg.norm(centres[i] - centres[j]), i, j)
                             for i in range(len(centres)) for j in range(i))
            if dist >= GROUP_MERGE:
                break
            centres[j] = (centres[i] * count[i] + centres[j] * count[j]) / (count[i] + count[j])
            count[j] += count[i]
            del centres[i], count[i]
        self.centres = np.array([c for c, k in zip(centres, count) if k >= GROUP_MIN * len(px)])
        targets = self._measure(lab, person, GROUP_MIN * len(px))
        ok = np.isfinite(targets[:, 0])
        self.centres, self.group_targets = (self.centres[ok], targets[ok]) if ok.any() else (None, None)

    def _measure(self, lab, person, min_pixels=400):
        """(groups, 3) Lab of each group in a frame: inter-quartile mean of L of the person pixels nearest to the
        group (pixels like none of the groups do not count), NaN where fewer than `min_pixels`."""
        px = lab[person]
        out = np.full((len(self.centres), 3), np.nan)
        if not len(px):
            return out
        d = ((_features(px)[:, None, :] - self.centres[None]) ** 2).sum(-1)
        label, near = d.argmin(1), d.min(1) < (3 * GROUP_MERGE) ** 2
        for k in range(len(self.centres)):
            g = px[(label == k) & near]
            if len(g) >= min_pixels:
                lo, hi = np.percentile(g[:, 0], [20, 80])
                g = g[(g[:, 0] >= lo) & (g[:, 0] <= hi)]
                out[k] = g.mean(0)
        return out

    def corrections(self, frames, keypoints=None, alphas=None):
        """Corrections for the next `frames` (uint8 RGB, in order; a chunk); None without a face in the reference.
        (N, 3) as (L gain, a shift, b shift) for the whole person; with colour groups and `alphas` (the frames'
        mattes) (N, 1 + groups, 3): the head's, then each group's, for Matting.composite(head=, groups=).
        With `keypoints` (the pose each frame was rendered from) the face is not detected again: same colour within
        0.4 L (median) on a 30s clip, 6 instead of 49 ms per frame."""
        if self.target is None:
            return None
        body = self.centres is not None and alphas is not None
        for i, frame in enumerate(frames):
            kp = self.detector(frame) if keypoints is None else keypoints[i]
            self.tracks[0].add(face_colour(frame, self.detector, kp), self.target)
            if body:
                for k, colour in enumerate(self._measure(*_body(frame, alphas[i], kp))):
                    if not self.skin[k]:
                        self.tracks[1 + k].add(colour, self.group_targets[k])
        head = np.nan_to_num(self.tracks[0].commit())  # nothing measured yet: no correction
        if not body:
            return np.c_[np.exp(head[:, 0]), head[:, 1:]]
        out = [head]
        for k, skin in enumerate(self.skin):  # within BODY_GAIN / BODY_SHIFT of the head; unmeasured: the head's
            rel = np.zeros_like(head) if skin else np.nan_to_num(self.tracks[1 + k].commit() - head)
            out.append(head + np.c_[np.clip(rel[:, :1], np.log(1 - BODY_GAIN), np.log(1 + BODY_GAIN)),
                                    np.clip(rel[:, 1:], -BODY_SHIFT, BODY_SHIFT)])
        out = np.stack(out, 1)
        out[..., 0] = np.exp(out[..., 0])
        return out


def _body(rgb, alpha, kp):
    """Half-resolution Lab of `rgb` and the opaque person in it outside the head and hands."""
    size = (rgb.shape[1] // SCALE, rgb.shape[0] // SCALE)
    lab = cv2.cvtColor(cv2.resize(rgb, size, interpolation=cv2.INTER_AREA), cv2.COLOR_RGB2LAB).astype(np.float32)
    a = cv2.resize(np.asarray(alpha, np.float32).reshape(rgb.shape[:2]), size, interpolation=cv2.INTER_AREA)
    head = cv2.resize(head_map(rgb.shape[:2], kp), size, interpolation=cv2.INTER_LINEAR)
    person = (a > 0.9) & (head < 0.1) & ~_hands_mask(rgb.shape[:2], kp, SCALE)
    return lab, person


class _Track:
    """One region's correction: desired per frame (log L gain, a shift, b shift towards the target; NaN where not
    measured) and the committed (applied) one."""

    def __init__(self):
        self.desired, self.committed = [], []

    def add(self, colour, target):
        self.desired.append(np.full(3, np.nan) if colour is None or not np.isfinite(colour[0]) else np.r_[
            np.log(np.clip(target[0] / max(colour[0], 1.0), *GAIN)), np.clip(target[1:] - colour[1:], -SHIFT, SHIFT)])

    def commit(self):
        """Corrections for the frames added since the last commit (a chunk), smoothed within that chunk only; NaN
        until anything was measured."""
        desired = _reject_outliers(np.array(self.desired))
        start, end = len(self.committed), len(self.desired)
        new = _smooth(desired, start, end)
        for j in range(len(new)):  # hold the last correction where nothing nearby could be measured
            if not np.isfinite(new[j, 0]):
                new[j] = new[j - 1] if j else (self.committed[-1] if self.committed else np.nan)
        self.committed.extend(new)
        return new


def _reject_outliers(d, window=6, k=3.5, floor=0.012):
    """NaN for single-frame glitches in the L correction (Hampel filter on log gain; floor ~1.2% of brightness)."""
    d = d.copy()
    gain = d[:, 0]
    for i in range(len(gain)):
        near = gain[max(0, i - window) : i + window + 1]
        near = near[np.isfinite(near)]
        if np.isfinite(gain[i]) and len(near) >= 3:
            median = np.median(near)
            spread = 1.4826 * np.median(np.abs(near - median))
            if abs(gain[i] - median) > max(k * spread, floor):
                d[i] = np.nan
    return d


def _smooth(d, start, end):
    """NaN-aware Gaussian-weighted local line through d[start:end] (one chunk), evaluated at each of its frames; NaN
    where too little was measured nearby. The window stops at the chunk's ends: the frames after it are not generated
    yet, and the ones before it were drawn by another chunk, which may light the person differently. A line rather
    than a mean, because a window cut at one side lagged a mean ~3 frames behind the model's drift (at a chunk start
    the model relaxes 2-6 L within ~10 frames)."""
    radius = int(np.ceil(3 * SIGMA))
    out = np.full((end - start, d.shape[1]), np.nan)
    for t in range(start, end):
        a, b = max(start, t - radius), min(end, t + radius + 1)
        x = np.arange(a, b) - t
        w = np.exp(-0.5 * (x / SIGMA) ** 2)
        ok = np.isfinite(d[a:b, 0])
        if not ok.any() or w[ok].sum() < 0.15 * w.sum():
            continue
        w, x, y = w[ok], x[ok], d[a:b][ok]
        mean = (w[:, None] * y).sum(0) / w.sum()
        centre = (w * x).sum() / w.sum()
        spread = (w * (x - centre) ** 2).sum() / w.sum()
        if spread > SIGMA ** 2 / 8:  # enough frames on either side to fit a slope
            mean -= centre * (w[:, None] * (x - centre)[:, None] * (y - mean)).sum(0) / (w.sum() * spread)
        out[t - start] = mean
    return out
