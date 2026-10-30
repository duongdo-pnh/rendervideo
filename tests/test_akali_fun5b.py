"""akali_bridge's Fun-5B branch (doc_api/AKALI_FUN5B.md): engines on claim, folder detection,
params, result folder hygiene, upload retries and the /complete | /fail | /cancel mapping.
akali and Drive are mocked; the render itself is simulated by editing the render job row."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import akali_bridge as ab
import database as db
import tts_db


class _TempDb(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        for target, attr, value in ((db, "DB_PATH", self.tmp / "jobs.db"), (tts_db, "DB_PATH", self.tmp / "jobs.db")):
            p = patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        db.init_db()
        tts_db.init_db()
        ab._init_batches_table()

    def folder(self, *names):
        d = self.tmp / "in"
        d.mkdir(exist_ok=True)
        for n in names:
            (d / n).write_bytes(b"x")
        return d


class ClaimEnginesTests(unittest.TestCase):
    def test_fun5b_only_when_installed_and_not_installing(self):
        import fun5b_adapter
        import fun5b_setup

        with patch.object(ab.gpu_info, "latentsync_blocked", return_value=False):
            with patch.object(fun5b_adapter, "unavailable_reason", return_value=(None, [])), \
                    patch.object(fun5b_setup, "running", return_value=False):
                self.assertEqual(ab._claim_engines(), ["musetalk", "latentsync", "fun5b"])
            with patch.object(fun5b_adapter, "unavailable_reason", return_value=(None, [])), \
                    patch.object(fun5b_setup, "running", return_value=True):
                self.assertEqual(ab._claim_engines(), ["musetalk", "latentsync"])
            with patch.object(fun5b_adapter, "unavailable_reason", return_value=("Chưa cài", ["x"])):
                self.assertEqual(ab._claim_engines(), ["musetalk", "latentsync"])

    def test_blocked_latentsync_and_broken_checks_never_break_claim(self):
        import fun5b_adapter

        with patch.object(ab.gpu_info, "latentsync_blocked", return_value=True), \
                patch.object(fun5b_adapter, "unavailable_reason", side_effect=OSError("boom")):
            self.assertEqual(ab._claim_engines(), ["musetalk"])

    def test_claim_sends_engines(self):
        with patch.object(ab, "_claim_engines", return_value=["musetalk", "fun5b"]), \
                patch.object(ab, "_base_url", return_value="http://akali"), \
                patch.object(ab, "_worker_key", return_value="k"), \
                patch.object(ab, "_worker_id", return_value="render-01"), \
                patch.object(ab.requests, "post") as post:
            post.return_value.status_code = 204
            self.assertIsNone(ab.claim())
        self.assertEqual(post.call_args.kwargs["json"], {"worker_id": "render-01", "engines": ["musetalk", "fun5b"]})


class IdentifyFun5bFilesTests(_TempDb):
    def test_akali_names_and_background(self):
        image, video, bg = ab._identify_fun5b_files(self.folder("nhan_vat.png", "video_mau.mp4", "background.jpg"))
        self.assertEqual((image.name, video.name, bg.name), ("nhan_vat.png", "video_mau.mp4", "background.jpg"))

    def test_any_names_other_files_ignored(self):
        image, video, bg = ab._identify_fun5b_files(self.folder("co gai.JPG", "mau.MOV", "ghi chu.xlsx"))
        self.assertEqual((image.name, video.name, bg), ("co gai.JPG", "mau.MOV", None))

    def test_wrong_counts_are_not_retryable(self):
        for names in (("a.mp4",), ("a.png", "b.png", "c.mp4"), ("a.png",), ("a.png", "b.mp4", "c.mp4"),
                      ("a.png", "b.mp4", "background.png", "background.jpg")):
            with self.subTest(names=names):
                d = self.tmp / "-".join(names)
                d.mkdir()
                for n in names:
                    (d / n).write_bytes(b"x")
                with self.assertRaises(ab.BridgeFailure) as err:
                    ab._identify_fun5b_files(d)
                self.assertFalse(err.exception.retryable)


class Fun5bOptionsTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(ab._fun5b_options({"engine": "fun5b"}), {
            "preset": "natural", "seed": 42, "keep_background": True, "auto_background": True,
            "table_line": None, "sharp_hands": False})

    def test_values_and_table_line_percent(self):
        opts = ab._fun5b_options({"preset": "fast", "seed": "7", "keep_background": False,
                                  "auto_background": "false", "table_line": 78, "sharp_hands": 1})
        self.assertEqual((opts["preset"], opts["seed"], opts["keep_background"], opts["auto_background"],
                          opts["sharp_hands"]), ("fast", 7, False, False, True))
        self.assertAlmostEqual(opts["table_line"], 0.78)
        self.assertIsNone(ab._fun5b_options({"table_line": 0})["table_line"])

    def test_bad_values_are_not_retryable(self):
        for params in ({"preset": "Tự nhiên"}, {"seed": "abc"}, {"table_line": 100}, {"table_line": "x"}):
            with self.subTest(params=params), self.assertRaises(ab.BridgeFailure) as err:
                ab._fun5b_options(params)
            self.assertFalse(err.exception.retryable)


class PrepareFun5bTests(_TempDb):
    def test_queues_one_render_job_and_cleans_the_result_folder(self):
        folder = self.folder("nhan_vat.png", "video_mau.mp4")
        with patch.object(ab.gdrive, "drive_enabled", return_value=True), \
                patch.object(ab, "_result_folder_id", return_value="FOLDER"), \
                patch.object(ab.gdrive, "trash_folder_contents", return_value=["old.mp4"]) as trash:
            batch_id, folder_name = ab._prepare_and_submit_fun5b(55, folder, {"engine": "fun5b", "table_line": 80})
        trash.assert_called_once_with("FOLDER")
        self.assertEqual(folder_name, "fun5b__job55")
        row = db.get_job(int(batch_id.removeprefix("fun5b:")))
        self.assertEqual((row["engine"], row["drive_folder"], row["status"]), ("fun5b", "fun5b__job55", "queued"))
        self.assertTrue(row["ref_image_path"].endswith("nhan_vat.png"))
        self.assertAlmostEqual(row["table_line"], 0.8)

    def test_no_drive_means_another_machine_should_take_it(self):
        with patch.object(ab.gdrive, "drive_enabled", return_value=False), \
                self.assertRaises(ab.BridgeFailure) as err:
            ab._prepare_and_submit_fun5b(55, self.folder("a.png", "b.mp4"), {})
        self.assertTrue(err.exception.retryable)
        self.assertEqual(db.list_jobs(), [])


class FinishFun5bTests(_TempDb):
    """_finish_fun5b against a render job row in each final state."""

    def setUp(self):
        super().setUp()
        self.job_id = db.add_fun5b_job("fun5b_job9", "/i.png", "/v.mp4", drive_folder="fun5b__job9")
        with ab._connect() as con:
            con.execute("INSERT INTO akali_batches (akali_job_id, claim_token, batch_id, excel_stem) VALUES (9, 't', ?, ?)",
                        (f"fun5b:{self.job_id}", "fun5b__job9"))
        self.calls = []
        for name in ("complete", "fail", "cancel"):
            p = patch.object(ab, name, side_effect=lambda *a, _n=name, **k: self.calls.append((_n, a, k)))
            p.start()
            self.addCleanup(p.stop)
        for attr, value in (("heartbeat", lambda *a, **k: None), ("_heartbeat_seconds", lambda: 0.01),
                            ("_abandon_batch", lambda *a, **k: self.calls.append(("abandon", a, {})))):
            p = patch.object(ab, attr, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(ab.time, "sleep")
        p.start()
        self.addCleanup(p.stop)

    def run_finish(self):
        ab._finish_fun5b(9, "t", f"fun5b:{self.job_id}", "fun5b__job9")
        with ab._connect() as con:
            return con.execute("SELECT status FROM akali_batches WHERE akali_job_id = 9").fetchone()["status"]

    def set_row(self, **cols):
        with db._connect() as con:
            con.execute(f"UPDATE jobs SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?", (*cols.values(), self.job_id))

    def test_done_cleans_folder_keeping_only_this_video_then_completes(self):
        self.set_row(status="done", output_path="/r/v.mp4", drive_link="https://drive.google.com/file/d/KEEP123/view")
        with patch.object(ab, "_result_folder_id", return_value="FOLDER"), \
                patch.object(ab.gdrive, "trash_folder_contents", return_value=["stray.mp4"]) as trash:
            status = self.run_finish()
        trash.assert_called_once_with("FOLDER", keep_file_id="KEEP123")
        self.assertEqual(self.calls, [("complete", (9, "t", "https://drive.google.com/drive/folders/FOLDER"), {})])
        self.assertEqual(status, "reported")

    def test_folder_cleanup_failure_keeps_lease_and_retries_later(self):
        self.set_row(status="done", drive_link="https://drive.google.com/file/d/K/view")
        with patch.object(ab, "_result_folder_id", side_effect=RuntimeError("drive down")):
            status = self.run_finish()
        self.assertEqual(self.calls, [])
        self.assertEqual(status, "running")

    def test_rejected_input_fails_without_retry(self):
        self.set_row(status="failed", error="[render_job] REJECTED: Không tìm thấy người trong ảnh")
        status = self.run_finish()
        (name, args, kwargs), = self.calls
        self.assertEqual((name, kwargs["retryable"]), ("fail", False))
        self.assertEqual(status, "abandoned")

    def test_other_render_failure_lets_another_machine_try(self):
        self.set_row(status="failed", error="render rc=1; CUDA error")
        self.run_finish()
        self.assertTrue(self.calls[0][2]["retryable"])

    def test_drive_quota_fails_at_once_without_retry(self):
        self.set_row(status="done", drive_error="<HttpError 403 ... The user's Drive storage quota has been exceeded.>")
        status = self.run_finish()
        (name, args, kwargs), = self.calls
        self.assertEqual((name, kwargs["retryable"]), ("fail", False))
        self.assertIn("hết dung lượng", args[2])
        self.assertEqual(status, "abandoned")

    def test_upload_error_is_retried_then_completes(self):
        self.set_row(status="done", output_path="/r/v.mp4", drive_error="timed out")
        clock = iter(range(0, 100000, 100))  # each loop turn: +100s, past every retry delay

        def reupload(row):
            db.set_drive_result(self.job_id, link="https://drive.google.com/file/d/NEW/view")

        with patch.object(ab.time, "monotonic", side_effect=lambda: next(clock)), \
                patch.object(ab, "_retry_fun5b_upload", side_effect=reupload) as retry, \
                patch.object(ab, "_result_folder_id", return_value="F"), \
                patch.object(ab.gdrive, "trash_folder_contents", return_value=[]):
            status = self.run_finish()
        retry.assert_called_once()
        self.assertEqual(self.calls[0][0], "complete")
        self.assertEqual(status, "reported")

    def test_upload_still_failing_after_all_retries_fails_retryable(self):
        self.set_row(status="done", output_path="/r/v.mp4", drive_error="timed out")
        clock = iter(range(0, 100000, 100))
        with patch.object(ab.time, "monotonic", side_effect=lambda: next(clock)), \
                patch.object(ab, "_retry_fun5b_upload", side_effect=RuntimeError("still down")) as retry:
            status = self.run_finish()
        self.assertEqual(retry.call_count, len(ab._FUN5B_UPLOAD_RETRY_DELAYS))
        (name, args, kwargs), = self.calls
        self.assertEqual((name, kwargs["retryable"]), ("fail", True))
        self.assertEqual(status, "abandoned")

    def test_cancelled_on_render_machine_reports_cancel(self):
        self.set_row(status="cancelled")
        self.run_finish()
        self.assertEqual([c[0] for c in self.calls], ["cancel", "abandon"])

    def test_canceled_on_akali_abandons_without_reporting(self):
        with patch.object(ab, "heartbeat", return_value="canceled"):
            self.run_finish()
        self.assertEqual([c[0] for c in self.calls], ["abandon"])

    def test_progress_follows_the_render_job(self):
        self.set_row(status="rendering", progress=0.5)
        self.assertEqual(ab._fun5b_progress(db.get_job(self.job_id)), 50)
        self.set_row(status="done")
        self.assertEqual(ab._fun5b_progress(db.get_job(self.job_id)), ab._FUN5B_UPLOADING_PROGRESS)


class DriveHelpersTests(unittest.TestCase):
    def test_file_id_from_link(self):
        g = ab.gdrive
        self.assertEqual(g.file_id_from_link("https://drive.google.com/file/d/1AbC_-9/view?usp=drivesdk"), "1AbC_-9")
        self.assertEqual(g.file_id_from_link("https://drive.google.com/open?id=XYZ"), "XYZ")
        self.assertIsNone(g.file_id_from_link(None))

    def test_trash_keeps_the_given_file(self):
        g = ab.gdrive
        files = [{"id": "A", "name": "a.mp4"}, {"id": "KEEP", "name": "k.mp4"}]
        with patch.object(g, "get_service") as svc, patch.object(g, "list_files", return_value=files):
            self.assertEqual(g.trash_folder_contents("F", keep_file_id="KEEP"), ["a.mp4"])
        svc.return_value.files.return_value.update.assert_called_once_with(
            fileId="A", body={"trashed": True}, supportsAllDrives=True)


if __name__ == "__main__":
    unittest.main()
