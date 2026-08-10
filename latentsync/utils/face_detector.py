from insightface.app import FaceAnalysis
import numpy as np
import torch

INSIGHTFACE_DETECT_SIZE = 512


def detect_size_for_frame(height: int, width: int) -> int:
    """Choose a larger InsightFace canvas for strongly rectangular video."""
    short_side = max(1, min(int(height), int(width)))
    long_side = max(int(height), int(width))
    aspect = long_side / short_side
    if aspect >= 1.7:
        return 1024
    if aspect >= 1.3:
        return 768
    return INSIGHTFACE_DETECT_SIZE


class FaceDetector:
    def __init__(self, device="cuda"):
        self.app = FaceAnalysis(
            allowed_modules=["detection", "landmark_2d_106"],
            root="checkpoints/auxiliary",
            providers=["CUDAExecutionProvider"],
        )
        self.ctx_id = cuda_to_int(device)
        self.det_size = None
        self.last_face_bbox = None

    def _prepare_for_frame(self, height: int, width: int) -> None:
        size = detect_size_for_frame(height, width)
        if size == self.det_size:
            return
        self.app.prepare(ctx_id=self.ctx_id, det_size=(size, size))
        self.det_size = size

    @staticmethod
    def _best_face(faces, threshold):
        best = None
        max_size = 0
        for face in faces:
            bbox = face.bbox.astype(np.int_).tolist()
            width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
            if width < 50 or height < 80:
                continue
            if width / height > 1.5 or width / height < 0.2:
                continue
            if face.det_score < threshold:
                continue
            size_now = width * height
            if size_now > max_size:
                max_size = size_now
                best = face
        return best

    def _tracking_roi(self, frame_bgr):
        if self.last_face_bbox is None:
            return None
        frame_h, frame_w = frame_bgr.shape[:2]
        x1, y1, x2, y2 = self.last_face_bbox
        width, height = x2 - x1, y2 - y1
        # Expand generously around the last face.  The cropped ROI is then fed
        # into the same detector canvas, making a small full-body face large.
        roi_x1 = max(0, int(x1 - width * 1.5))
        roi_y1 = max(0, int(y1 - height * 1.5))
        roi_x2 = min(frame_w, int(x2 + width * 1.5))
        roi_y2 = min(frame_h, int(y2 + height * 1.5))
        if roi_x2 <= roi_x1 or roi_y2 <= roi_y1:
            return None
        return frame_bgr[roi_y1:roi_y2, roi_x1:roi_x2], (roi_x1, roi_y1)

    def __call__(self, frame, threshold=0.5):
        f_h, f_w, _ = frame.shape
        self._prepare_for_frame(f_h, f_w)

        # The video readers return RGB, while InsightFace expects OpenCV BGR.
        # Channel reversal needs a contiguous copy because it has negative strides.
        frame_bgr = np.ascontiguousarray(np.asarray(frame)[..., ::-1])
        face = self._best_face(self.app.get(frame_bgr), threshold)
        offset_x = offset_y = 0

        # Root fix for full-body video: once a face has been acquired, retry a
        # missed full-frame detection inside an expanded tracked ROI.  This
        # preserves real detection/landmarks instead of blindly copying a frame.
        if face is None:
            tracked = self._tracking_roi(frame_bgr)
            if tracked is not None:
                roi, (offset_x, offset_y) = tracked
                face = self._best_face(self.app.get(roi), threshold)
        if face is None:
            return None, None

        raw_bbox = face.bbox.astype(np.int_).copy()
        raw_bbox[[0, 2]] += offset_x
        raw_bbox[[1, 3]] += offset_y
        self.last_face_bbox = raw_bbox.tolist()

        lmk = np.round(face.landmark_2d_106).astype(np.int_)
        lmk[:, 0] += offset_x
        lmk[:, 1] += offset_y

        halk_face_coord = np.mean([lmk[74], lmk[73]], axis=0)  # lmk[73]

        sub_lmk = lmk[LMK_ADAPT_ORIGIN_ORDER]
        halk_face_dist = np.max(sub_lmk[:, 1]) - halk_face_coord[1]
        upper_bond = halk_face_coord[1] - halk_face_dist  # *0.94

        x1, y1, x2, y2 = (np.min(sub_lmk[:, 0]), int(upper_bond), np.max(sub_lmk[:, 0]), np.max(sub_lmk[:, 1]))

        if y2 - y1 <= 0 or x2 - x1 <= 0 or x1 < 0:
            x1, y1, x2, y2 = raw_bbox.tolist()

        y2 += int((x2 - x1) * 0.1)
        x1 -= int((x2 - x1) * 0.05)
        x2 += int((x2 - x1) * 0.05)

        x1 = max(0, x1)
        y1 = max(0, y1)
        x2 = min(f_w, x2)
        y2 = min(f_h, y2)

        return (x1, y1, x2, y2), lmk


def cuda_to_int(cuda_str: str) -> int:
    """
    Convert the string with format "cuda:X" to integer X.
    """
    if cuda_str == "cuda":
        return 0
    device = torch.device(cuda_str)
    if device.type != "cuda":
        raise ValueError(f"Device type must be 'cuda', got: {device.type}")
    return device.index


LMK_ADAPT_ORIGIN_ORDER = [
    1,
    10,
    12,
    14,
    16,
    3,
    5,
    7,
    0,
    23,
    21,
    19,
    32,
    30,
    28,
    26,
    17,
    43,
    48,
    49,
    51,
    50,
    102,
    103,
    104,
    105,
    101,
    73,
    74,
    86,
]
