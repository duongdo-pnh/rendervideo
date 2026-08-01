import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from musetalk_streaming.models import StreamConfig
from musetalk_streaming.output import FileStreamOutput, mask_push_url
from musetalk_streaming.session import SentenceRequest, StreamSession, half_frame_threshold


class StreamingTests(unittest.TestCase):
    def test_playout_waits_for_half_of_total_frames(self):
        self.assertEqual(half_frame_threshold(945, 25), 473)
        self.assertEqual(half_frame_threshold(600, 25), 300)
        self.assertEqual(half_frame_threshold(1, 25), 1)

    def test_realtime_playout_uses_configured_warmup_not_half(self):
        with tempfile.TemporaryDirectory() as directory:
            config = StreamConfig(
                "s", "a", "/tmp/avatar.mp4", "rtmp://host/live/key",
                fps=15, width=16, height=16, warmup_frames=1,
                video_queue_frames=4,
            )
            frame = np.zeros((16, 16, 3), dtype=np.uint8)
            session = StreamSession(
                config, FileStreamOutput(directory), [frame], lambda *_: None
            )
            session._current = type("Request", (), {"total_frames": 100})()
            session._packets.put(object())
            self.assertTrue(session._ready_to_play())

    def test_mask_push_url(self):
        self.assertEqual(
            mask_push_url("rtmp://user:pass@example.com/live/secret-key?token=x"),
            "rtmp://example.com/live/***",
        )

    def test_config_accepts_realtime_15_fps(self):
        config = StreamConfig(
            "s", "a", "/tmp/a.mp4", "rtmp://host/live/key", fps=15
        )
        self.assertEqual(config.fps, 15)

    def test_config_rejects_fps_outside_safe_range(self):
        with self.assertRaises(ValueError):
            StreamConfig("s", "a", "/tmp/a.mp4", "rtmp://host/live/key", fps=9)
        with self.assertRaises(ValueError):
            StreamConfig("s", "a", "/tmp/a.mp4", "rtmp://host/live/key", fps=31)

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

    def test_uploaded_audio_is_deleted_after_render(self):
        with tempfile.TemporaryDirectory() as directory:
            audio_path = Path(directory) / "upload.wav"
            audio_path.write_bytes(b"temporary audio")
            config = StreamConfig(
                "s", "a", "/tmp/avatar.mp4", "rtmp://host/live/key",
                width=16, height=16, warmup_frames=1, video_queue_frames=4,
            )
            frame = np.zeros((16, 16, 3), dtype=np.uint8)

            def producer(request, emit, cancel):
                request.total_frames = 1
                emit(frame, np.ones(640, dtype=np.int16))

            session = StreamSession(
                config, FileStreamOutput(Path(directory) / "output"), [frame], producer
            )
            session.start()
            session.enqueue("upload", str(audio_path), delete_after_use=True)
            deadline = time.monotonic() + 2
            while audio_path.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            session.stop()
            self.assertFalse(audio_path.exists())

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

    def test_comment_preemption_requeues_current_audio_from_played_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            config = StreamConfig(
                "s", "a", "/tmp/avatar.mp4", "rtmp://host/live/key",
                width=16, height=16, warmup_frames=1, video_queue_frames=4,
            )
            frame = np.zeros((16, 16, 3), dtype=np.uint8)
            session = StreamSession(
                config, FileStreamOutput(directory), [frame], lambda *_: None
            )
            current = SentenceRequest(
                priority=10, sequence=0, request_id="product",
                audio_path="/tmp/product.wav", delete_after_use=True,
                start_frame=100, total_frames=500, played_frames=75,
            )
            session._current = current
            session._last_driver_frame_index = 42
            session._packets.put(type(
                "Packet", (), {
                    "request_id": "product",
                    "frame_path": "/tmp/missing-1.jpg",
                    "driver_frame_index": 43,
                }
            )())
            session._packets.put(type(
                "Packet", (), {
                    "request_id": "other",
                    "frame_path": "/tmp/missing-2.jpg",
                    "driver_frame_index": 99,
                }
            )())

            next_driver_frame_index = session.interrupt(
                clear_pending=False, resume_current=True
            )

            resumed = session._sentence_heap[0]
            self.assertEqual(resumed.start_frame, 176)
            self.assertIsNone(resumed.start_driver_frame_index)
            self.assertEqual(next_driver_frame_index, 44)
            self.assertTrue(resumed.delete_after_use)
            self.assertFalse(current.delete_after_use)
            self.assertEqual(session._packets.qsize(), 2)


if __name__ == "__main__":
    unittest.main()
