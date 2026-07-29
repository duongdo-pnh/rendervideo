import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from musetalk_streaming.models import StreamConfig
from musetalk_streaming.output import FileStreamOutput, mask_push_url
from musetalk_streaming.session import StreamSession, half_frame_threshold


class StreamingTests(unittest.TestCase):
    def test_playout_waits_for_half_of_total_frames(self):
        self.assertEqual(half_frame_threshold(945, 25), 473)
        self.assertEqual(half_frame_threshold(600, 25), 300)
        self.assertEqual(half_frame_threshold(1, 25), 1)

    def test_mask_push_url(self):
        self.assertEqual(
            mask_push_url("rtmp://user:pass@example.com/live/secret-key?token=x"),
            "rtmp://example.com/live/***",
        )

    def test_config_rejects_non_25_fps(self):
        with self.assertRaises(ValueError):
            StreamConfig("s", "a", "/tmp/a.mp4", "rtmp://host/live/key", fps=30)

    def test_normalize_frame_preserves_source_aspect_ratio(self):
        with tempfile.TemporaryDirectory() as directory:
            config = StreamConfig(
                "s", "a", "/tmp/avatar.mp4", "rtmp://host/live/key",
                width=16, height=16, warmup_frames=1, video_queue_frames=4,
            )
            wide = np.full((8, 16, 3), 255, dtype=np.uint8)
            session = StreamSession(config, FileStreamOutput(directory), [wide], lambda *_: None)
            normalized = session.get_preview_frame()
            self.assertEqual(normalized.shape, (16, 16, 3))
            self.assertTrue(np.all(normalized[:4] == 0))
            self.assertTrue(np.all(normalized[4:12] == 255))
            self.assertTrue(np.all(normalized[12:] == 0))

    def test_file_playout_keeps_av_clock_aligned(self):
        with tempfile.TemporaryDirectory() as directory:
            config = StreamConfig(
                "s", "a", "/tmp/avatar.mp4", "rtmp://host/live/key",
                width=16, height=16, warmup_frames=1, video_queue_frames=4,
            )
            frame = np.zeros((16, 16, 3), dtype=np.uint8)

            def producer(request, emit, cancel):
                for _ in range(2):
                    emit(frame, np.ones(640, dtype=np.int16))

            output = FileStreamOutput(directory)
            session = StreamSession(config, output, [frame], producer)
            session.start()
            session.enqueue("r1", "/tmp/audio.wav")
            time.sleep(0.18)
            status = session.get_status()
            session.stop()
            self.assertLessEqual(abs(status["av_drift_ms"]), 0.01)
            self.assertGreaterEqual(status["output_fps"], 10)
            self.assertTrue((Path(directory) / "timeline.jsonl").is_file())


if __name__ == "__main__":
    unittest.main()
