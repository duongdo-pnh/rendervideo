"""
Background lock: Robust Video Matting (temporally stable person alpha) puts every generated frame back onto a clean
plate made from the reference image, so the green screen, table and lighting stay exactly as in the reference
instead of drifting from chunk to chunk; the person's brightness and colour cast are matched to the reference.
"""

import os

import cv2
import numpy as np
import torch

from fun5b import config

HUB_DIR = os.path.join(config.CKPT_DIR, "wan22-fun-5b/rvm/hub")  # torch.hub cache: RVM code + rvm_resnet50.pth
# RVM's internal resolution (×640×1152). At 0.25 (~176×264) it lost outstretched arms against a warm pink wall: the
# arms were composited half-transparent and the reference's arm stayed in the plate as a ghost. 0.5+ keeps them.
DOWNSAMPLE = 0.75
# Big-LaMa (TorchScript, as used by IOPaint) paints the backdrop behind the person: shelves, panels and light strips
# continue with detail. TELEA/row interpolation left smooth blobs that showed wherever the person moved away.
MID_TONES = (70, 240)  # OpenCV Lab L range the colour lock measures the person on (see _lab_stats)
LOCK_RATE = 1 / 30  # per-frame smoothing of the colour lock (time constant ~1s at 30fps)
LAMA = os.path.join(config.CKPT_DIR, "wan22-fun-5b/lama/big-lama.pt")
HEAD_DIFF = 20  # _person_shape: Lab distance from the clean background that counts as the person
HEAD_LIMIT = 1.6  # Matting.hold: no hold when the head shape is this many times RVM's head (a misaligned background)
HAIR_COLOURS, HAIR_LIKE = 4, (0.3, 0.7)  # _solid: k-means colours of a held part, the ramp of d_plate / (d_plate +
#   d_part)
HEAD_GROW = (12, 40)  # _solid: growth past the head shape (px): through any hair-like pixel, then only through
#   those RVM saw something in
HAIR_APART = 20  # Matting.hold: a part's colour this far (Lab) at least from the face's skin and the scene behind it
HEAD_FADE = 10  # Matting.held: frames the hold fades in and out over as the face points come and go
SLEEVE_RADIUS, SLEEVE_LENGTH, SLEEVE_OUT = 0.26, 1.0, 0.1  # _sleeves: of the shoulder width, of the way from shoulder
#   to elbow, of the shoulder width
SLEEVE_CORE = 0.18  # _sleeves radius of the arm core, see _solid's `core`
SLEEVE_GROW = (12, 20)  # as HEAD_GROW, for the sleeves (a cuff band with its lining is ~12px)
SLEEVE_DIFFER, SLEEVE_SEEN, SLEEVE_NEAR = (12, 24), 0.03, 12  # _solid: a sleeve pixel also counts as the person this
#   far from the plate (Lab ramp; the model's own background next to the arms is within ~5 of it, p90 15) where RVM's
#   alpha is over SLEEVE_SEEN or within SLEEVE_NEAR px of the sleeve's colours
FACE_POINTS, POSE_SCORE = np.r_[24:92], 0.3  # DWPose face points (as fun5b.facelock.FACE) and their score threshold


class Matting:
    def __init__(self):
        torch.hub.set_dir(HUB_DIR)
        code = os.path.join(HUB_DIR, "PeterL1n_RobustVideoMatting_master")
        self.model = torch.hub.load(code, "resnet50", source="local").eval().cuda()
        self.lama = torch.jit.load(LAMA, map_location="cpu").eval()  # on the GPU only while painting a plate
        self.reset()

    def reset(self):
        self.rec = [None] * 4  # recurrent state: one video at a time, frames in order
        self.lock = None  # smoothed (L gain, a/b shift) of composite()'s colour lock

    @torch.no_grad()
    def __call__(self, frame):
        """uint8 RGB frame -> (foreground uint8 RGB, alpha float32 HxWx1), continuing the current video."""
        x = torch.from_numpy(frame).cuda().permute(2, 0, 1)[None].float() / 255
        fgr, pha, *self.rec = self.model(x, *self.rec, downsample_ratio=DOWNSAMPLE)
        fgr = (fgr[0].permute(1, 2, 0).clamp(0, 1) * 255).round().byte().cpu().numpy()
        return fgr, pha[0].permute(1, 2, 0).float().cpu().numpy()

    @torch.no_grad()
    def held(self, frame, matte, plate, pose):
        """`matte` (this frame's (fgr, alpha) from __call__) with the head and the sleeves held solid (see hold), for
        a frame composited on `plate` with keypoints `pose`; frames in order. The matte as it is with nothing held."""
        head, sleeves = getattr(self, "head", None), getattr(self, "sleeves", None)
        if head is None and sleeves is None:
            return matte
        if getattr(self, "_held_plate", None) is not plate:  # one upload per video
            self._held_plate, self._held_lab = plate, _lab(torch.from_numpy(plate).cuda().float() / 255)
        x = torch.from_numpy(frame).cuda().float() / 255
        lab = _lab(x)
        fgr = torch.from_numpy(matte[0]).cuda().float() / 255
        alpha = torch.from_numpy(np.asarray(matte[1], np.float32)).cuda().reshape(*frame.shape[:2], 1)
        if head is not None:
            move = _face_move(head[1], pose)
            if move is not None:
                self._head_move = move
            # Faded in and out over HEAD_FADE frames (no pop when the face points come and go); the last fit is kept
            # while the face is hidden.
            self._head_weight = float(np.clip(self._head_weight + (1 if move is not None else -1) / HEAD_FADE, 0, 1))
            if self._head_move is not None and self._head_weight > 0:
                prior = cv2.warpAffine(head[0], self._head_move, (frame.shape[1], frame.shape[0])) * self._head_weight
                fgr, alpha = _solid(x, lab, fgr, alpha, self._held_lab, prior, head[2], HEAD_GROW)
        if sleeves is not None:  # faded like the head when the shoulders or elbows drop out
            arms = _sleeves(pose, frame.shape[:2], sleeves[1])
            if arms is not None:
                self._sleeve_arms = arms
                self._sleeve_core = _sleeves(pose, frame.shape[:2], sleeves[1], out=0.0, radius=SLEEVE_CORE)
            step = (1 if arms is not None else -1) / HEAD_FADE
            self._sleeve_weight = float(np.clip(self._sleeve_weight + step, 0, 1))
            if self._sleeve_arms is not None and self._sleeve_weight > 0:
                fgr, alpha = _solid(x, lab, fgr, alpha, self._held_lab, self._sleeve_arms * self._sleeve_weight,
                                    sleeves[0], SLEEVE_GROW, SLEEVE_DIFFER, connected=True, core=self._sleeve_core,
                                    floor=getattr(self, "table_row", None))
        return (fgr * 255).round().byte().cpu().numpy(), alpha.cpu().numpy()

    def hold(self, pose):
        """Keep the reference person's hair and sleeves solid in every frame from now on (Matting.held) where RVM
        loses them but the model drew them. `pose`: the reference's own keypoints; None holds nothing. Only with a
        clean background (a painted-out plate differs from the reference around the person).
        Hair: RVM lost a light-brown bun against a light wall, in the reference itself (alpha < 0.9) and for whole
        stretches of a render (alpha 0.1-0.3 for 50+ frames while the model drew it solid), and the wall showed
        through it as a grey patch on her head. The head's shape is where the reference differs from the clean
        background (exact outside the person: mean Lab distance 0.35), above the chin, feathered; its colours are
        those of that shape outside the face. Each frame moves the shape with the face keypoints. Not when the shape
        is far bigger than RVM's head (a background photo that does not line up with the reference).
        Sleeves: RVM gave a solid green sleeve with crisp cuff stripes alpha 0.3-0.8 whenever the arm moved fast (the
        shelves behind showed through it; TMF clip: 120 frames with over 300 such pixels, none after). Each frame's
        upper arms (_sleeves, from its pose, shoulder to elbow, moved out past the shoulder joint); the colours are
        the reference's inside its own upper arms, and a pixel far from the plate next to them or where RVM saw
        something counts too (the white cuff stripes, too close to the light shelves to be a sleeve colour, were lost
        otherwise); only what joins the sleeve's colours is kept.
        Either way only the pixels that look like the part rather than the plate are kept (_solid), and the part's
        colours leave out anything like her skin or the scene behind the part."""
        self.head, self.sleeves, self._head_move, self._head_weight = None, None, None, 1.0
        self._sleeve_arms, self._sleeve_core, self._sleeve_weight = None, None, 1.0
        if getattr(self, "person_shape", None) is None or pose is None:
            return
        if self.person_shape.sum() > HEAD_LIMIT * max(float((self.ref_alpha > 0.5).sum()), 1.0):
            return  # the background does not line up with the reference: the shapes and the plate test are wrong
        face = pose[FACE_POINTS]
        seen = face[:, 2] > POSE_SCORE
        if seen.sum() < 20:
            return
        skin = np.median(_lab_pixels(self.reference[_hull(face[seen, :2], self.ref_alpha.shape) > 0]), axis=0)
        self.sleeves = self._sleeve_colours(pose, skin)
        x, y = face[seen, 0], face[seen, 1]
        width = x.max() - x.min()
        region = np.zeros_like(self.person_shape)
        region[: int(y.max()), max(int(x.min() - width / 2), 0) : int(x.max() + width / 2)] = 1
        shape = self.person_shape * region
        if shape.sum() > HEAD_LIMIT * max(float(((self.ref_alpha > 0.5) * region).sum()), 1.0):
            return
        hair = (shape > 0) & (cv2.dilate(_hull(face[seen, :2], shape.shape), np.ones((11, 11), np.uint8)) == 0)
        # Not the forehead's skin (above the face points, inside the shape) nor anything like the wall behind the
        # head: with those, a wall pixel the model drew a little off the plate counted as hair.
        centres = self._colours(hair, (region > 0) & (shape == 0), skin)
        if centres is None:
            return
        feather = cv2.GaussianBlur(cv2.dilate(region, np.ones((3, 3), np.uint8)), (0, 0), 8)  # no step at the box
        self.head = (cv2.GaussianBlur(shape, (0, 0), 1.5) * feather, face, centres)

    def _sleeve_colours(self, pose, skin):
        """(the reference's sleeve colours, a (k, 3) Lab tensor; its shoulder width) for hold(), or None."""
        arms = _sleeves(pose, self.ref_alpha.shape)
        if arms is None:
            return None
        arms = arms > 0.5
        sleeve = arms & (self.ref_alpha > 0.95)
        around = cv2.dilate(arms.astype(np.uint8), np.ones((15, 15), np.uint8)).astype(bool) & (self.ref_alpha < 0.05)
        colours = self._colours(sleeve, around, skin)
        return None if colours is None else (colours, float(np.linalg.norm(pose[2, :2] - pose[5, :2])))

    def _colours(self, part, around, skin):
        """HAIR_COLOURS k-means colours of the reference's pixels in `part`, without those within HAIR_APART of `skin`
        or of the clean background's median over `around`: a (k, 3) Lab tensor on the GPU, or None."""
        if part.sum() < 200:
            return None
        cv2.setRNGSeed(0)
        stop = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.1)
        _, _, centres = cv2.kmeans(_lab_pixels(self.reference[part]), HAIR_COLOURS, None, stop, 3,
                                   cv2.KMEANS_PP_CENTERS)
        behind = [np.median(_lab_pixels(self.background[around]), axis=0)] if around.any() else []
        centres = [c for c in centres if all(np.linalg.norm(c - colour) > HAIR_APART for colour in [skin, *behind])]
        return torch.from_numpy(np.array(centres, np.float32)).cuda() if centres else None

    def plate(self, reference, table_line=None, background=None):
        """The reference with the person painted out by LaMa, or `background` (the same scene without the person,
        same size as `reference`) when there is one: LaMa blurs a hole as big as a person. Also records the person's
        colour statistics in the reference, the target of composite()'s colour lock."""
        self.reset()
        for _ in range(6):  # let the recurrent state settle on the still image
            fgr, alpha = self(reference)
        self.reset()
        self.ref_stats = _lab_stats(fgr, alpha)
        self.ref_alpha = alpha[..., 0]  # the body colour groups of fun5b.facelock are found in the reference with it
        self.front, self.front_rgb = _front_layer(alpha[..., 0], table_line), reference.astype(np.float32)
        self.table_row = _table_row(alpha[..., 0] > 0.5, table_line)
        self.table_known = _table_known(alpha[..., 0] > 0.5, self.table_row)
        self.person_shape = None if background is None else _person_shape(reference, background, alpha[..., 0])
        self.reference, self.background, self.head, self.sleeves = reference, background, None, None
        if background is not None:  # the scene is complete behind her (table and products included): nothing needs to
            self.front = _front_layer(np.zeros(alpha.shape[:2], np.float32), table_line)  # cover her hands, only a set line
            return background
        # A wide margin around the matte: a tight one leaves a skin/hair halo for the inpainting to continue.
        mask = (alpha[..., 0] > 0.02).astype(np.uint8)
        mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (45, 45)))
        return _match_surroundings(self._inpaint(reference, mask), reference, mask.astype(bool))

    @torch.no_grad()
    def _inpaint(self, image, mask):
        """uint8 RGB image, uint8 0/1 mask -> uint8 RGB with the masked pixels painted by LaMa."""
        height, width = mask.shape
        pad = lambda n: (-n) % 8  # noqa: E731  (LaMa needs multiples of 8)
        x = torch.from_numpy(image).permute(2, 0, 1)[None].float().div(255)
        m = torch.from_numpy(mask)[None, None].float()
        x = torch.nn.functional.pad(x, (0, pad(width), 0, pad(height)), mode="reflect").cuda()
        m = torch.nn.functional.pad(m, (0, pad(width), 0, pad(height)), mode="reflect").cuda()
        self.lama.cuda()
        try:
            out = self.lama(x, m)[0, :, :height, :width]
        finally:
            self.lama.cpu()
            torch.cuda.empty_cache()
        out = (out.permute(1, 2, 0).clamp(0, 1) * 255).round().byte().cpu().numpy()
        keep = mask[..., None] == 0  # pixels outside the mask stay exactly the reference's
        return np.where(keep, image, out)

    def composite(self, frame, plate, lock=None, matte=None, head=None, groups=None):
        """Person from `frame` on `plate`, its brightness and colour cast matched to the reference: with `lock`
        (L gain, a shift, b shift; fun5b.facelock) as given, else to the person in the reference, smoothed over ~1s
        (a per-frame correction flickered with whatever the model drew that frame). A lock per region (1 + groups, 3)
        takes `head` (soft head map, any scale: facelock.head_map) and `groups` (FaceLock.centres): the head gets the
        first row, every other pixel the rows of the colour groups it belongs to. `matte`: the frame's (foreground,
        alpha) from __call__, when the frames were matted first (the lock is measured on the matte)."""
        if lock is not None:
            return self._composite_gpu(frame, plate, lock, matte, head, groups)
        fgr, alpha = self(frame) if matte is None else (matte[0], np.asarray(matte[1], np.float32))
        if lock is None:
            (mean, _), (t_mean, _) = _lab_stats(fgr, alpha), self.ref_stats
            lock = np.r_[np.clip(t_mean[0] / max(mean[0], 1.0), 0.8, 1.25), t_mean[1:] - mean[1:]]
            lock = lock if self.lock is None else self.lock + (lock - self.lock) * LOCK_RATE
        self.lock = lock
        fgr = _match_lab(fgr, lock[0], lock[1:])
        out = fgr * alpha + plate * (1 - alpha)
        out = out * (1 - self.front) + self.front_rgb * self.front  # table and products in front stay on top
        return out.round().astype(np.uint8)

    @torch.no_grad()
    def _composite_gpu(self, frame, plate, lock, matte=None, head=None, groups=None):
        """composite() with a given lock, on the GPU: the numpy Lab round trip and blending took 71 of 103 ms per
        frame (now 31 ms with RVM). Within 3 levels of the CPU path at p99 (no uint8 rounding in between)."""
        self.lock = lock
        if getattr(self, "_plate_src", None) is not plate:  # one upload per video
            self._plate_src = plate
            self._plate = torch.from_numpy(plate).cuda().float()
            self._front = torch.from_numpy(self.front).cuda()
            self._front_rgb = torch.from_numpy(self.front_rgb).cuda()
        if matte is None:
            x = torch.from_numpy(frame).cuda().permute(2, 0, 1)[None].float() / 255
            fgr, pha, *self.rec = self.model(x, *self.rec, downsample_ratio=DOWNSAMPLE)
            fgr, alpha = fgr[0].permute(1, 2, 0).clamp(0, 1), pha[0].permute(1, 2, 0).float()
        else:
            fgr = torch.from_numpy(matte[0]).cuda().float() / 255
            alpha = torch.from_numpy(np.asarray(matte[1], np.float32)).cuda().reshape(fgr.shape[0], fgr.shape[1], 1)
        lock = np.asarray(lock, np.float64)
        if lock.ndim == 2:
            fgr = _relight(fgr, *_region_lock(fgr, lock, head, groups))
        else:
            fgr = _relight(fgr, float(lock[0]), float(lock[1]), float(lock[2]))
        out = fgr * 255 * alpha + self._plate * (1 - alpha)
        out = out * (1 - self._front) + self._front_rgb * self._front
        return out.round().clamp(0, 255).byte().cpu().numpy()


def _front_layer(alpha, table_line=None, margin=2, feather=1.5):
    """
    What is in front of the person in the reference (a table and the products on it): everything below the person's
    lower edge, which is where the table cuts them off. The edge is taken per column, carried to the image sides and
    median-smoothed. Composited on top of the person it keeps those objects exactly as in the reference; otherwise the
    model redraws them blurred and the person's matte pulls them in. Empty when the person reaches the bottom of the
    frame (no table). Returns an HxWx1 float mask.
    `table_line` (fraction of the height, optional) also puts everything below that row in front, person matte or
    not: a product right under the person (black shorts on the counter below her waist) is matted as her lower body
    by every model tried (RVM, Mask R-CNN, DWPose's hips), and the model then redraws it.
    """
    height, width = alpha.shape
    front = _person_front(alpha > 0.5, margin)
    if table_line:
        front |= np.arange(height)[:, None] >= round(table_line * height)
    front = cv2.GaussianBlur(front.astype(np.float32), (0, 0), feather)
    return front[..., None]


def _table_row(person, table_line=None):
    """The row where the table starts under the person in the reference (fun5b.run gives the model the table from
    there down as known pixels, so it draws her down to it): `table_line` when set, else the lowest flat run of her
    lower edge (her body cut by the table). Not the median edge: with her arms spread, most columns end at the arms
    (row 685 of 1152 where the counter starts at 746), and the known "table" then covered her waist and the model
    erased it (a green blur in its place). None without a table under her, or when the rows below the found line
    still hold more than 1% of her (hands resting on the table, a slanted table): nothing is given then."""
    height = person.shape[0]
    if table_line:
        return round(table_line * height)
    below = _person_front(person, 2)
    if not below.any():
        return None
    hem = np.where(below.any(axis=0), below.argmax(axis=0), -1)
    sits = np.array([h for x, h in enumerate(hem) if h > 12 and person[h - 12 : h - 2, x].mean() > 0.5])
    if len(sits) < 20:
        return None
    low = np.percentile(sits, 90)
    flat = sits[np.abs(sits - low) <= 3]
    if len(flat) < 20:
        return None
    row = int(np.median(flat))
    if person[row + 2 :].sum() > 0.01 * person.sum():
        return None
    return row


def _table_known(person, table_row, block=16):
    """The first row given to the video model as known pixels (fun5b.run._give_table): the whole latent row (16px) at
    or ABOVE the table line, so the last few rows of her body just above the table are given too and the model has
    no free strip between her and the table. With the row below the line instead (6 free rows on a pharmacy counter
    at 746) it drew its own beige counter lip there in whole chunks, cut her 15-16px above the counter and she
    floated over a strip of the plate (274 of 420 frames over 10px); from the row above, 3px in every frame, the
    reference's own. Only when those rows hold her torso alone (a hand or an arm there would be frozen): else the row
    below the line."""
    if table_row is None:
        return None
    top = table_row // block * block
    if top == table_row:
        return top
    band = person[top:table_row]
    torso = person[max(table_row - 12, 0) : table_row - 2].mean(axis=0) > 0.5  # her body sits on the table there
    stray = band[:, ~torso].sum()
    return top if stray <= 0.02 * band.size else -(-table_row // block) * block


def _person_front(person, margin):
    """The automatic part of _front_layer: below the person's lower edge, as an HxW bool mask."""
    from scipy.ndimage import median_filter

    height, width = person.shape
    cols = np.where(person.any(axis=0))[0]
    if len(cols) == 0:
        return np.zeros((height, width), bool)
    bottom = np.empty(width)
    bottom[cols] = height - 1 - np.argmax(person[::-1, cols], axis=0)
    bottom[: cols[0]], bottom[cols[-1] + 1 :] = bottom[cols[0]], bottom[cols[-1]]
    inner = np.interp(np.arange(width), cols, bottom[cols])  # fill gaps between person columns
    bottom = median_filter(inner, size=31, mode="nearest")
    if np.median(bottom[cols]) >= height - 1 - margin:
        return np.zeros((height, width), bool)
    return (np.arange(height)[:, None] > bottom[None, :] + margin) & ~person


def _lab_stats(rgb, alpha):
    """Per-channel mean and std in Lab over the person's mid-tones (alpha-weighted). Dark hair and clothes and
    near-white highlights are left out: how much of them a frame shows (hair swinging, an arm over the top) moved the
    mean, and the colour lock then brightened or darkened the whole person, face included."""
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32).reshape(-1, 3)
    mid = ((lab[:, 0] >= MID_TONES[0]) & (lab[:, 0] <= MID_TONES[1]))[:, None]
    w = alpha.reshape(-1, 1) * (alpha.reshape(-1, 1) > 0.5) * mid
    w = w / max(float(w.sum()), 1.0)
    mean = (lab * w).sum(axis=0)
    std = np.sqrt((((lab - mean) ** 2) * w).sum(axis=0)) + 1e-3
    return mean, std


def _match_lab(rgb, gain, shift):
    """Apply the colour lock without touching the black point: L is scaled (black stays black; shifting and
    stretching it lifted the blacks to grey, a washed-out look), a/b are shifted. composite() clamps the gain to ±25%
    so a frame that legitimately shows more skin or vest is not pushed too far."""
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
    lab[..., 0] *= gain
    lab[..., 1:] += shift
    return cv2.cvtColor(np.clip(lab, 0, 255).round().astype(np.uint8), cv2.COLOR_LAB2RGB)


def _match_surroundings(plate, reference, hole, sigma=24):
    """`plate` with the large-scale colour of `hole` replaced by the background around it, carried inwards by a
    pull-push fill (coarser and coarser averages of the known pixels until the hole is covered, then back up); LaMa's
    finer detail stays. On a green screen LaMa painted the hole a greyer, bluer green with a dark ghost of the person
    in it (L -5, a +6, b -8 against the backdrop), and that showed as a washed-out halo around the person wherever
    they moved."""
    around = _pull_push(reference.astype(np.float32), (~hole).astype(np.float32))
    blur = lambda x: cv2.GaussianBlur(x, (0, 0), sigma)  # noqa: E731
    shift = blur(around) - blur(plate.astype(np.float32))
    fade = cv2.GaussianBlur(hole.astype(np.float32), (0, 0), 8)[..., None]  # no seam at the hole's edge
    return np.clip(plate + shift * fade, 0, 255).round().astype(np.uint8)


def _pull_push(image, weight):
    """`image` (HxWx3 float) with the pixels of zero `weight` filled from their known surroundings."""
    if weight.min() > 0 or min(weight.shape) < 4:
        return image
    small = (weight.shape[1] // 2, weight.shape[0] // 2)
    w = cv2.resize(weight, small, interpolation=cv2.INTER_AREA)
    coarse = cv2.resize(image * weight[..., None], small, interpolation=cv2.INTER_AREA) / np.maximum(w, 1e-6)[..., None]
    coarse = _pull_push(coarse, (w > 0).astype(np.float32))
    up = cv2.resize(coarse, (weight.shape[1], weight.shape[0]), interpolation=cv2.INTER_LINEAR)
    return np.where(weight[..., None] > 0, image, up)


# sRGB <-> CIE Lab (D65) as OpenCV computes it, in torch; a/b shifts are in OpenCV 8-bit units.
_RGB2XYZ = torch.tensor([[0.412453, 0.357580, 0.180423], [0.212671, 0.715160, 0.072169],
                         [0.019334, 0.119193, 0.950227]])
# Inverted on the CPU, once: inverting on the GPU per frame needs cuSOLVER, which failed to start mid-render.
_XYZ2RGB = torch.linalg.inv(_RGB2XYZ)
_WHITE = torch.tensor([0.950456, 1.0, 1.088754])


def _lab(rgb):
    """HxWx3 float RGB in 0..1 (GPU) -> CIE Lab (L 0..100)."""
    m, white = _RGB2XYZ.to(rgb.device), _WHITE.to(rgb.device)
    linear = torch.where(rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4)
    xyz = linear @ m.T / white
    f = torch.where(xyz > 0.008856, xyz.clamp(min=1e-12) ** (1 / 3), 7.787 * xyz + 16 / 116)
    return torch.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], -1)


def _region_lock(fgr, lock, head, centres):
    """Per-pixel (gain, shift a, shift b) maps (3xHxW, GPU) from a lock per region (fun5b.facelock): the head map's
    pixels take the head's row, the others the rows of the colour groups their colour is close to (soft membership
    in facelock's group space: a hand over the scrubs takes the skin's correction, the sleeve beside it the
    scrubs'). Blurred over ~2px, so noise in the membership does not lift or sink single pixels against their
    neighbours."""
    from fun5b import facelock

    lab = _lab(fgr)
    feat = torch.stack([lab[..., 0] * 2.55 * facelock.L_WEIGHT, lab[..., 1], lab[..., 2]], -1)  # OpenCV 8-bit units
    centres = torch.as_tensor(np.asarray(centres, np.float32), device=fgr.device)
    centres = centres - torch.tensor([0.0, 128.0, 128.0], device=fgr.device)
    d = ((feat[..., None, :] - centres) ** 2).sum(-1)
    member = torch.softmax(-d / (2 * facelock.GROUP_SPREAD ** 2), -1)  # (H, W, groups)
    body = member @ torch.as_tensor(lock[1:], dtype=torch.float32, device=fgr.device)  # (H, W, 3)
    hw = torch.as_tensor(np.asarray(head, np.float32), device=fgr.device)
    if hw.shape != fgr.shape[:2]:
        hw = torch.nn.functional.interpolate(hw[None, None], size=fgr.shape[:2], mode="bilinear")[0, 0]
    corr = body + (torch.as_tensor(lock[0], dtype=torch.float32, device=fgr.device) - body) * hw[..., None]
    k = torch.exp(-0.5 * (torch.arange(-6, 7, device=fgr.device, dtype=torch.float32) / 2) ** 2)
    k = k / k.sum()
    c = corr.permute(2, 0, 1)[:, None]
    c = torch.nn.functional.conv2d(torch.nn.functional.pad(c, (6, 6, 0, 0), mode="replicate"), k.view(1, 1, 1, -1))
    c = torch.nn.functional.conv2d(torch.nn.functional.pad(c, (0, 0, 6, 6), mode="replicate"), k.view(1, 1, -1, 1))
    return c[:, 0, :, :]  # gain, shift a, shift b


def _person_shape(reference, background, alpha):
    """Where `reference` is not `background` (Lab distance over a 5px mean above HEAD_DIFF, small specks opened away)
    or RVM's reference alpha is solid: the person with every part RVM missed. HxW float32."""
    lab = lambda x: cv2.cvtColor(x, cv2.COLOR_RGB2LAB).astype(np.float32) * [100 / 255, 1, 1]  # noqa: E731
    differs = cv2.blur(np.linalg.norm(lab(reference) - lab(background), axis=-1), (5, 5)) > HEAD_DIFF
    shape = (differs | (alpha > 0.9)).astype(np.uint8)
    return cv2.morphologyEx(shape, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8)).astype(np.float32)


def _lab_pixels(pixels):
    """(N, 3) uint8 RGB -> (N, 3) float32 Lab (_lab's)."""
    return _lab(torch.from_numpy(pixels).float()[None] / 255)[0].numpy().astype(np.float32)


def _hull(points, shape):
    """uint8 HxW mask of the convex hull of `points` (N, 2)."""
    mask = np.zeros(shape, np.uint8)
    cv2.fillPoly(mask, [cv2.convexHull(points.astype(np.int32))], 1)
    return mask


def _sleeves(pose, shape, ref_width=None, out=None, radius=None):
    """Soft HxW float32 map of the sleeves of a frame with keypoints `pose`: each upper arm from the shoulder
    SLEEVE_LENGTH of the way to the elbow, moved SLEEVE_OUT x the shoulder width out from the body (a sleeve's outer
    edge is 40-60px past the shoulder joint), 2 x SLEEVE_RADIUS x the shoulder width thick (the shoulder width kept
    within 0.7-1.3x `ref_width`, the reference's, against a stray shoulder point), feathered 3px; None without both
    shoulders. `out`, `radius`: instead of SLEEVE_OUT, SLEEVE_RADIUS."""
    out_by, radius = SLEEVE_OUT if out is None else out, SLEEVE_RADIUS if radius is None else radius
    shoulders, elbows = (2, 5), (3, 6)  # DWPose body points (OpenPose order)
    if (pose[list(shoulders), 2] <= POSE_SCORE).any():
        return None
    width = np.linalg.norm(pose[2, :2] - pose[5, :2])
    if ref_width:
        width = float(np.clip(width, 0.7 * ref_width, 1.3 * ref_width))
    arms = np.zeros(shape, np.float32)
    middle = (pose[2, :2] + pose[5, :2]) / 2
    for s, e in zip(shoulders, elbows):
        if pose[e, 2] > POSE_SCORE:
            away = pose[s, :2] - middle  # the sleeve puffs out past the shoulder joint, away from the body
            away = out_by * width * away / max(np.linalg.norm(away), 1e-6)
            start, end = pose[s, :2] + away, pose[s, :2] + away + SLEEVE_LENGTH * (pose[e, :2] - pose[s, :2])
            thick = max(int(2 * radius * width), 1)
            cv2.line(arms, tuple(int(v) for v in start), tuple(int(v) for v in end), 1.0, thick)
    return cv2.GaussianBlur(arms, (0, 0), 3) if arms.any() else None


def _face_move(ref_face, pose):
    """The similarity (2x3) taking the reference's face points onto those of `pose`, fitted on the points seen in
    both; None when fewer than 20 are seen."""
    face = pose[FACE_POINTS]
    seen = (face[:, 2] > POSE_SCORE) & (ref_face[:, 2] > POSE_SCORE)
    if seen.sum() < 20:
        return None
    move, _ = cv2.estimateAffinePartial2D(ref_face[seen, :2].astype(np.float32), face[seen, :2].astype(np.float32))
    return move


def _solid(frame, lab, fgr, alpha, plate_lab, prior, colours, grow, differ=None, connected=False, core=None,
           floor=None):
    """
    RVM's (fgr, alpha) with a part held solid (the hair, the sleeves): alpha is raised to `prior` (where the part
    is on this frame), grown past it (the model draws a bun bigger or shifted: 20px taller for 50 frames of the TMF
    clip) by grow[0] px, then on to grow[1] px where RVM's alpha is not 0, times how much the pixel looks like the
    part rather than the plate there: d_plate / (d_plate + d_part) in Lab (d_part: to the nearest of the part's
    colours in the reference, `colours`), ramped over HAIR_LIKE; growth only through pixels past the ramp's middle.
    A pixel the model drew as wall, even a wall unlike the plate (it redraws the wall near the head and the sign),
    stays the plate, and a strand mixed with the wall keeps a partial alpha. Where alpha is raised, the frame's own
    colour replaces RVM's foreground (estimated for a blend). Not where the plate itself has the part's colour.
    `connected`: only what joins the part's colours inside `prior` (the sleeves: not a stray piece the model drew
    beside the arm), rather than all of `prior`. `core` (HxW numpy): where the part's colours may make far-from-plate
    pixels next to them count (not at the arm's outer edge, where a shelf pillar or box edge the model drew a few px
    off stood next to the sleeve: a grey column beside the elbow for 100 frames). `floor`: nothing is held from this
    row down (the table and the products on it: held box lids under the elbows showed the model's own redraw of them,
    ghosted and flickering).
    frame/fgr HxWx3 and alpha HxWx1 float tensors on the GPU (0..1), lab = _lab(frame), plate_lab HxWx3, prior HxW
    numpy, colours (k, 3) Lab tensor.
    """
    f = torch.nn.functional
    to_plate = (lab - plate_lab).norm(dim=-1)
    to_part = (lab[..., None, :] - colours).norm(dim=-1).amin(-1)
    like = f.avg_pool2d((to_plate / (to_plate + to_part + 1e-3))[None, None], 3, 1, 1)  # no ragged edge from noise
    like = ((like - HAIR_LIKE[0]) / (HAIR_LIKE[1] - HAIR_LIKE[0])).clamp(0, 1)
    # Where the plate itself has the part's colour (green boxes or plants behind a green sleeve) a see-through part
    # does not show, and holding it would only put the model's redraw of the scene there.
    like = like * ((plate_lab[..., None, :] - colours).norm(dim=-1).amin(-1) > HAIR_APART).float()
    prior = torch.from_numpy(prior).cuda()[None, None]
    seed = prior * (like > 0.5).float()  # the part's own colours inside `prior`
    if differ is not None:
        # Or simply far from the plate (the sleeves' white cuff stripes, too close to the light shelves to be a sleeve
        # colour) where RVM saw something or next to the part's colours: not the shelf's light strip, a box edge the
        # model drew a few px off, or a stray ghost cuff it drew beside the arm, which RVM rightly left out.
        far = ((f.avg_pool2d(to_plate[None, None], 3, 1, 1) - differ[0]) / (differ[1] - differ[0])).clamp(0, 1)
        inner = seed if core is None else seed * (torch.from_numpy(core).cuda()[None, None] > 0.5).float()
        near = torch.maximum((alpha[..., 0] > SLEEVE_SEEN).float()[None, None],
                             f.max_pool2d(inner, 2 * SLEEVE_NEAR + 1, 1, SLEEVE_NEAR))
        like = torch.maximum(like, far * near)
    clear = (like > 0.5).float()
    seen = clear * (alpha[..., 0] > 0.05).float()[None, None]
    grown = seed if connected else prior * clear
    for step in range(grow[1]):  # one pixel per pass, only through pixels that look like the part
        grown = torch.maximum(grown, f.max_pool2d(grown, 3, 1, 1) * (clear if step < grow[0] else seen))
    grown = f.avg_pool2d(grown, 3, 1, 1)
    held = f.avg_pool2d((grown if connected else torch.maximum(prior, grown)) * like, 5, 1, 2)  # no jagged outline
    if floor is not None:  # faded out over the 4 rows above it
        rows = torch.arange(held.shape[-2], device=held.device, dtype=torch.float32)[:, None]
        held = held * ((floor - rows) / 4).clamp(0, 1)
    solid = torch.maximum(alpha[..., 0], held[0, 0])[..., None]
    given = (solid - alpha) / (1 - alpha).clamp(min=1e-3)  # the share of the missing alpha given back
    return fgr + (frame - fgr) * given, solid


def _relight(rgb, gain, shift_a, shift_b):
    """HxWx3 float RGB in 0..1 (GPU), with Lab L x gain, a + shift_a, b + shift_b (floats, or HxW maps); clipped like
    _match_lab."""
    inverse, white = _XYZ2RGB.to(rgb.device), _WHITE.to(rgb.device)
    lab = _lab(rgb)
    lab = torch.stack([(lab[..., 0] * gain).clamp(0, 100), (lab[..., 1] + shift_a).clamp(-128, 127),
                       (lab[..., 2] + shift_b).clamp(-128, 127)], -1)
    fy = (lab[..., 0] + 16) / 116
    f = torch.stack([fy + lab[..., 1] / 500, fy, fy - lab[..., 2] / 200], -1)
    xyz = torch.where(f > 0.206893, f ** 3, (f - 16 / 116) / 7.787) * white
    linear = (xyz @ inverse.T).clamp(0, 1)
    return torch.where(linear <= 0.0031308, linear * 12.92, 1.055 * linear ** (1 / 2.4) - 0.055).clamp(0, 1)
