import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from latentsync.tts.autovoice import AutoVoiceError, AutoVoiceTTS
import tts_errors


WAV = b"RIFF" + b"\x00" * 64


def response(status, content=b"", content_type="application/json", retry_after=None):
    r = Mock()
    r.status_code = status
    r.content = content
    r.text = content.decode("utf-8", errors="replace")
    r.headers = {"Content-Type": content_type}
    if retry_after is not None:
        r.headers["Retry-After"] = str(retry_after)
    return r


class AutoVoiceTests(unittest.TestCase):
    def provider(self, **kwargs):
        return AutoVoiceTTS(
            api_key="secret", default_voice="pv-default", timeout=60,
            retries_503=kwargs.pop("retries_503", 3), **kwargs
        )

    @patch("latentsync.tts.autovoice.requests.post")
    def test_passes_custom_voice_id_and_saves_raw_wav(self, post):
        post.return_value = response(200, WAV, "audio/wav")
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "voice.wav"
            self.provider().synthesize("hello", str(out), "pv-new-voice")
            self.assertEqual(out.read_bytes(), WAV)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["voiceId"], "pv-new-voice")
        self.assertNotIn("X-API-Key", str(payload))

    @patch("latentsync.tts.autovoice.time.sleep")
    @patch("latentsync.tts.autovoice.requests.post")
    def test_retries_only_503_and_honors_retry_after(self, post, sleep):
        post.side_effect = [
            response(503, b'{"category":"tts_capacity_unavailable"}', retry_after=2),
            response(503, b'{"category":"tts_capacity_unavailable"}'),
            response(200, WAV, "audio/wav"),
        ]
        with tempfile.TemporaryDirectory() as td:
            self.provider().synthesize("hello", str(Path(td) / "voice.wav"), "pv-new")
        self.assertEqual(post.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2.0, 5])

    @patch("latentsync.tts.autovoice.requests.post")
    def test_does_not_retry_401(self, post):
        post.return_value = response(401, b'{"message":"Invalid API key"}')
        with self.assertRaises(AutoVoiceError) as caught:
            self.provider().synthesize("hello", "/tmp/unused.wav", "pv-new")
        self.assertEqual(post.call_count, 1)
        self.assertFalse(tts_errors.is_retryable(caught.exception))

    def test_configured_voices_are_dynamic(self):
        p = self.provider(configured_voices='{"English":"pv-en","Thai":"pv-th"}')
        self.assertEqual(p.voices(), ["pv-default", "pv-en", "pv-th"])


if __name__ == "__main__":
    unittest.main()
