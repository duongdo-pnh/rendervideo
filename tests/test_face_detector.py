import unittest
from unittest.mock import patch

import numpy as np

from latentsync.utils import face_detector as fd


class _FakeFaceAnalysis:
    def __init__(self, **_kwargs):
        self.prepare_calls = []
        self.frames = []

    def prepare(self, **kwargs):
        self.prepare_calls.append(kwargs)

    def get(self, frame):
        self.frames.append(frame.copy())
        return []


class FaceDetectorTests(unittest.TestCase):
    def test_detect_size_follows_aspect_ratio(self):
        self.assertEqual(fd.detect_size_for_frame(1000, 1000), 512)
        self.assertEqual(fd.detect_size_for_frame(1300, 1000), 768)
        self.assertEqual(fd.detect_size_for_frame(1920, 1080), 1024)
        self.assertEqual(fd.detect_size_for_frame(1080, 1920), 1024)

    @patch.object(fd, "FaceAnalysis", _FakeFaceAnalysis)
    def test_detector_converts_rgb_to_bgr_and_prepares_once(self):
        detector = fd.FaceDetector("cuda")
        rgb = np.array([[[10, 20, 30]]], dtype=np.uint8)

        self.assertEqual(detector(rgb), (None, None))
        self.assertEqual(detector(rgb), (None, None))

        self.assertEqual(detector.app.prepare_calls, [{"ctx_id": 0, "det_size": (512, 512)}])
        self.assertEqual(detector.app.frames[0].tolist(), [[[30, 20, 10]]])

    @patch.object(fd, "FaceAnalysis", _FakeFaceAnalysis)
    def test_tracking_roi_expands_previous_face(self):
        detector = fd.FaceDetector("cuda")
        detector.last_face_bbox = [80, 100, 120, 160]
        roi, offset = detector._tracking_roi(np.zeros((300, 200, 3), dtype=np.uint8))
        self.assertEqual(offset, (20, 10))
        self.assertEqual(roi.shape, (240, 160, 3))

    @patch.object(fd, "FaceAnalysis", _FakeFaceAnalysis)
    def test_detector_reprepares_when_frame_ratio_changes(self):
        detector = fd.FaceDetector("cuda")
        detector(np.zeros((100, 100, 3), dtype=np.uint8))
        detector(np.zeros((192, 108, 3), dtype=np.uint8))
        self.assertEqual(
            [call["det_size"] for call in detector.app.prepare_calls],
            [(512, 512), (1024, 1024)],
        )


if __name__ == "__main__":
    unittest.main()
