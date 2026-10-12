"""Cổng Node key — máy chưa nhập INGEST_TOKEN thì không làm gì được.

Hai lớp chặn: middleware require_node_key (backend/app.py) khoá /api, và
dashboard_reporter.wait_until_activated() chặn 3 tiến trình nền.
"""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import dashboard_reporter as dr


class DashboardEnabledTests(unittest.TestCase):
    def test_only_the_token_decides(self):
        # URL đã cố định trong code nên không còn là điều kiện.
        with patch.object(dr, "_env", lambda name, default="": "" if name == "INGEST_TOKEN" else "x"):
            self.assertFalse(dr.dashboard_enabled())
        with patch.object(dr, "_env", lambda name, default="": "tok" if name == "INGEST_TOKEN" else ""):
            self.assertTrue(dr.dashboard_enabled())

    def test_url_falls_back_to_the_baked_in_default(self):
        with patch.object(dr, "_env", lambda name, default="": ""):
            self.assertEqual(dr.dashboard_url(), dr._DEFAULT_DASHBOARD_URL)
        with patch.object(dr, "_env", lambda name, default="": "https://dev.example"):
            self.assertEqual(dr.dashboard_url(), "https://dev.example")


class WaitUntilActivatedTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.env_path = Path(tmp.name) / ".env"
        self.env_path.write_text("NODE_NAME=may-test\n")
        for attr, value in (("ENV_PATH", self.env_path), ("_ENV_MTIME", 0.0),
                            ("NODE_KEY_POLL_SECONDS", 0.05)):
            patcher = patch.object(dr, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.dict("os.environ", {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        import os
        os.environ.pop("INGEST_TOKEN", None)

    def test_returns_immediately_when_already_activated(self):
        self.env_path.write_text("INGEST_TOKEN=tok\n")
        self.assertTrue(dr.wait_until_activated(lambda: True, lambda _m: None))

    def test_blocks_until_the_token_appears_without_a_restart(self):
        """Chờ chứ không thoát: thoát thì không có gì khởi động lại worker sau khi
        người dùng nhập token."""
        done = {}

        def run():
            done["ok"] = dr.wait_until_activated(lambda: True, lambda _m: None)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        time.sleep(0.3)
        self.assertNotIn("ok", done, "không được đi tiếp khi chưa có token")

        self.env_path.write_text("NODE_NAME=may-test\nINGEST_TOKEN=tok\n")
        thread.join(timeout=5)

        self.assertTrue(done.get("ok"), "phải tự phát hiện token mới, không cần restart")

    def test_gives_up_when_asked_to_shut_down(self):
        self.assertFalse(dr.wait_until_activated(lambda: False, lambda _m: None))


class MiddlewareAllowlistTests(unittest.TestCase):
    def test_only_the_paths_the_activation_screen_needs_stay_open(self):
        from backend.app import _NODE_KEY_OPEN_PATHS

        # Đọc trạng thái + lưu token + health: thiếu 1 trong 3 là màn hình kích
        # hoạt tự khoá chính nó, không bao giờ nhập được token.
        self.assertEqual(
            _NODE_KEY_OPEN_PATHS,
            frozenset({"/api/health", "/api/dashboard/status", "/api/dashboard/config"}),
        )


if __name__ == "__main__":
    unittest.main()
