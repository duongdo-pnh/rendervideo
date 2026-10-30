"""Automatic Fun-5B install after an update (fun5b_setup.py, install/setup_fun5b.sh)."""
import fcntl
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fun5b_setup as fs
import gpu_info


class _TempLogs(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        for attr, name in (("LOCK", ".lock"), ("PROGRESS", "progress"), ("RESULT", "result"), ("LOG", "setup.log")):
            p = patch.object(fs, attr, self.tmp / name)
            p.start()
            self.addCleanup(p.stop)


class AutoStartTests(_TempLogs):
    def test_nothing_happens_when_installed(self):
        with patch.object(fs, "installed", return_value=True), patch.object(fs, "start") as start:
            fs.auto_start()
        start.assert_not_called()

    def test_small_gpu_is_skipped_and_remembered_without_downloading(self):
        with patch.object(fs, "installed", return_value=False), patch.object(fs, "start") as start, \
                patch.object(gpu_info, "gpu_total_gib", return_value=12.0), \
                patch.object(gpu_info, "current_gpu_name", return_value="RTX 3080 Ti"):
            fs.auto_start()
            status = fs.status()
        start.assert_not_called()
        self.assertEqual(status["state"], "skipped")
        self.assertIn("RTX 3080 Ti", status["message"])

    def test_missing_engine_on_a_big_gpu_starts_the_install(self):
        with patch.object(fs, "installed", return_value=False), patch.object(fs, "start") as start, \
                patch.object(fs, "preflight", return_value=(0, "ok")):
            fs.auto_start()
        start.assert_called_once()

    def test_a_failing_check_never_breaks_backend_start(self):
        with patch.object(fs, "installed", side_effect=OSError("boom")):
            fs.auto_start()  # must not raise

    def test_not_started_twice_while_running(self):
        with open(fs.LOCK, "a+") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            with patch.object(fs, "installed", return_value=False), patch.object(fs, "start") as start:
                fs.auto_start()
            start.assert_not_called()


class PreflightTests(unittest.TestCase):
    def test_not_enough_disk_is_a_retryable_failure(self):
        with patch.object(gpu_info, "gpu_total_gib", return_value=24.0), \
                patch.object(fs, "bytes_needed", return_value=80e9), \
                patch.object(fs.shutil, "disk_usage", return_value=type("U", (), {"free": 30e9})()):
            code, reason = fs.preflight()
        self.assertEqual(code, 1)
        self.assertIn("30 GB", reason)

    def test_no_gpu_is_skipped(self):
        with patch.object(gpu_info, "gpu_total_gib", return_value=0.0):
            self.assertEqual(fs.preflight()[0], 3)


class StatusTests(_TempLogs):
    def test_running_reports_step_and_model_download(self):
        fs.PROGRESS.write_text("6|7|Tải model Fun-5B và FireRed (~57 GB)\n")
        with open(fs.LOCK, "a+") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX)
            with patch.object(fs, "models_bytes", return_value=20e9):
                s = fs.status()
        self.assertEqual((s["state"], s["step"], s["total"], s["downloaded_gb"]), ("running", 6, 7, 20.0))

    def test_failed_result_is_shown_until_installed(self):
        fs.RESULT.write_text("failed|Lỗi ở bước: Tải model — xem logs/fun5b-setup.log\n")
        with patch.object(fs, "installed", return_value=False):
            self.assertEqual(fs.status()["state"], "failed")
        with patch.object(fs, "installed", return_value=True):
            self.assertEqual(fs.status()["state"], "installed")


class ScriptTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parent.parent

    def test_scripts_parse(self):
        for script in ("install/setup_fun5b.sh", "install/download_fun5b_models.sh"):
            subprocess.run(["bash", "-n", str(self.ROOT / script)], check=True)

    def test_locks_pin_every_package(self):
        for lock in ("install/fun5b-requirements.lock", "install/fun5b-edit-requirements.lock"):
            lines = [line for line in (self.ROOT / lock).read_text().splitlines() if line and not line.startswith("#")]
            self.assertTrue(lines)
            self.assertTrue(all("==" in line for line in lines), lock)


if __name__ == "__main__":
    unittest.main()
