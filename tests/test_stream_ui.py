import unittest
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from stream_ui import _compose_push_url, _normalize_facebook_avatar


class StreamUiTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
    def test_normalize_preserves_landscape_portrait_and_square(self):
        import cv2

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            output = Path(directory) / "normalized.mp4"
            for width, height, expected in (
                (640, 360, (1280, 720)),
                (360, 640, (720, 1280)),
                (400, 300, (960, 720)),
                (320, 320, (720, 720)),
            ):
                with self.subTest(size=(width, height)):
                    subprocess.run([
                        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"color=c=white:s={width}x{height}:r=24:d=0.25",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
                    ], check=True)
                    with patch("stream_ui.FACEBOOK_AVATAR_720", output):
                        result = _normalize_facebook_avatar(str(source))
                    cap = cv2.VideoCapture(result)
                    try:
                        ok, frame = cap.read()
                        self.assertTrue(ok)
                        self.assertEqual((frame.shape[1], frame.shape[0]), expected)
                        self.assertAlmostEqual(cap.get(cv2.CAP_PROP_FPS), 30)
                        # White reaches every edge: no black portrait padding.
                        self.assertGreater(int(frame.min()), 230)
                    finally:
                        cap.release()

    def test_composes_facebook_url_without_query(self):
        self.assertEqual(
            _compose_push_url(
                "rtmps://live-api-s.facebook.com:443/rtmp/",
                "secret-stream-key",
            ),
            "rtmps://live-api-s.facebook.com:443/rtmp/secret-stream-key",
        )

    def test_rejects_missing_key(self):
        with self.assertRaises(Exception):
            _compose_push_url("rtmps://live-api-s.facebook.com:443/rtmp/", "")

    def test_rejects_credentials_in_server_url(self):
        with self.assertRaises(Exception):
            _compose_push_url("rtmps://user:pass@example.com/rtmp", "key")


if __name__ == "__main__":
    unittest.main()
