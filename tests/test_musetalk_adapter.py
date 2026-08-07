import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from musetalk_adapter import _wait_for_response


class MuseTalkAdapterTests(unittest.TestCase):
    def test_wait_emits_heartbeat_before_response(self):
        class Client:
            def setblocking(self, _value):
                pass

            def recv(self, _size):
                return b'{"ok": true}\n'

        client = Client()
        output = StringIO()
        with patch("musetalk_adapter.select.select",
                   side_effect=[([], [], []), ([client], [], [])]), redirect_stdout(output):
            line = _wait_for_response(client, heartbeat_seconds=30, max_render_seconds=7200)
        self.assertEqual(line, '{"ok": true}')
        self.assertIn("still rendering", output.getvalue())

    def test_wait_has_two_hour_style_total_deadline(self):
        class Client:
            def setblocking(self, _value):
                pass

        with patch("musetalk_adapter.select.select", return_value=([], [], [])), \
             patch("musetalk_adapter.time.monotonic", side_effect=[0, 0, 7201]):
            with self.assertRaises(TimeoutError):
                _wait_for_response(Client(), heartbeat_seconds=30, max_render_seconds=7200)


if __name__ == "__main__":
    unittest.main()
