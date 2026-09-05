import unittest

from stream_ui import _compose_push_url


class StreamUiTests(unittest.TestCase):
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
