import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd

import akali_bridge as ab
import database as db
import excel_import as xi
import tts_db


class RowFinishedTests(unittest.TestCase):
    def test_tts_terminal_failures_are_finished(self):
        self.assertTrue(ab._row_finished("failed_permanent", None, None))
        self.assertTrue(ab._row_finished("failed_retryable", None, None))

    def test_render_failed_is_finished(self):
        self.assertTrue(ab._row_finished("done", "failed", None))

    def test_render_done_without_drive_result_still_waiting(self):
        # mark_done() chạy trước _start_drive_upload() (thread nền) — render_status
        # có thể là 'done' trong khi drive_link/drive_error vẫn còn None một lúc.
        self.assertFalse(ab._row_finished("done", "done", {"drive_link": None, "drive_error": None}))

    def test_render_done_with_drive_link_or_error_is_finished(self):
        self.assertTrue(ab._row_finished("done", "done", {"drive_link": "x", "drive_error": None}))
        self.assertTrue(ab._row_finished("done", "done", {"drive_link": None, "drive_error": "boom"}))

    def test_still_in_progress_is_not_finished(self):
        self.assertFalse(ab._row_finished("pending", None, None))
        self.assertFalse(ab._row_finished("submitting", None, None))

    def test_cancelled_render_is_finished(self):
        # Thiếu nhánh này thì một dòng bị huỷ giữ finished < total vĩnh viễn và
        # bridge treo trên batch đó cho tới khi mất lease.
        self.assertTrue(ab._row_finished("done", "cancelled", None))


class ConflictReasonTests(unittest.TestCase):
    @staticmethod
    def _error(status, body=None, bad_json=False):
        response = Mock()
        response.status_code = status
        response.json = Mock(side_effect=ValueError) if bad_json else Mock(return_value=body)
        return Mock(response=response)

    def test_reads_reason_from_409_body(self):
        err = self._error(409, {"error": "job da bi huy", "reason": "canceled"})
        self.assertEqual(ab._conflict_reason(err), "canceled")
        self.assertEqual(ab._conflict_reason(self._error(409, {"reason": "lease_lost"})), "lease_lost")

    def test_non_409_returns_none(self):
        self.assertIsNone(ab._conflict_reason(self._error(500, {"reason": "canceled"})))

    def test_409_without_usable_body_is_unknown(self):
        self.assertEqual(ab._conflict_reason(self._error(409, bad_json=True)), "unknown")
        self.assertEqual(ab._conflict_reason(self._error(409, {})), "unknown")


class DeleteUnstartedInBatchTests(unittest.TestCase):
    def test_removes_only_rows_that_would_still_generate_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(tts_db, "DB_PATH", Path(tmp) / "jobs.db"):
                tts_db.init_db()
                ids = {}
                for status in ("pending", "submitting", "retry_wait", "done", "failed_permanent"):
                    ids[status] = tts_db.enqueue(
                        "batch-x", 2, "text", "autovoice", "voice", "sp", "/v.mp4",
                        "gioi_thieu", "", render_config=None,
                    )
                    with tts_db._connect() as con:
                        con.execute("UPDATE tts_jobs SET status=? WHERE id=?", (status, ids[status]))
                other = tts_db.enqueue("batch-khac", 2, "t", "autovoice", "v", "sp", "/v.mp4",
                                       "gioi_thieu", "", render_config=None)

                removed = tts_db.delete_unstarted_in_batch("batch-x")

                self.assertEqual(removed, 3)
                with tts_db._connect() as con:
                    left = {r["id"] for r in con.execute("SELECT id FROM tts_jobs")}
                self.assertEqual(left, {ids["done"], ids["failed_permanent"], other})


class RequestCancelByDriveFolderTests(unittest.TestCase):
    def test_queued_cancelled_outright_rendering_flagged_done_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "jobs.db"):
                db.init_db()

                def make(name, folder, status):
                    job_id = db.add_job(name, "/v.mp4", "/a.wav", "512", drive_folder=folder)
                    with db._connect() as con:
                        con.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
                    return job_id

                queued = make("q", "FAQ__job9", "queued")
                rendering = make("r", "FAQ__job9", "rendering")
                finished = make("d", "FAQ__job9", "done")
                other = make("o", "FAQ__job10", "queued")

                touched = db.request_cancel_by_drive_folder("FAQ__job9")

                self.assertEqual(set(touched), {queued, rendering})
                self.assertEqual(db.get_job(queued)["status"], "cancelled")
                self.assertEqual(db.get_job(rendering)["status"], "rendering")
                self.assertEqual(db.get_job(rendering)["cancel_requested"], 1)
                self.assertEqual(db.get_job(finished)["status"], "done")
                self.assertEqual(db.get_job(other)["status"], "queued")

    def test_no_matching_batch_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", Path(tmp) / "jobs.db"):
                db.init_db()
                self.assertEqual(db.request_cancel_by_drive_folder("khong-co"), [])


class BatchCancelFlagTests(unittest.TestCase):
    """Cờ huỷ cấp batch — tín hiệu để bridge biết 'huỷ cả batch' thay vì phải đoán
    từ trạng thái từng dòng."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.object(db, "DB_PATH", Path(self._tmp.name) / "jobs.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        db.init_db()
        ab._init_batches_table()

    def test_flag_is_off_until_requested_then_only_for_that_batch(self):
        ab._save_batch(80, "tok-80", "batch-80", "FAQ__job80")
        ab._save_batch(81, "tok-81", "batch-81", "FAQ__job81")
        self.assertFalse(ab._batch_cancel_requested(80))

        marked = ab.request_batch_cancel(80)

        self.assertEqual(marked, [(80, "FAQ__job80")])
        self.assertTrue(ab._batch_cancel_requested(80))
        self.assertFalse(ab._batch_cancel_requested(81))

    def test_request_all_covers_every_running_batch_but_not_finished_ones(self):
        ab._save_batch(80, "tok-80", "batch-80", "FAQ__job80")
        ab._save_batch(81, "tok-81", "batch-81", "FAQ__job81")
        ab._mark_batch(81, "reported")

        marked = ab.request_batch_cancel()

        self.assertEqual([job_id for job_id, _ in marked], [80])
        self.assertFalse(ab._batch_cancel_requested(81))

    def test_requeued_job_starts_with_a_clean_flag(self):
        # akali giao lại cùng akali_job_id sau khi lượt trước bị huỷ. Không xoá cờ
        # thì lượt mới bị huỷ oan ngay vòng lặp đầu.
        ab._save_batch(80, "tok-cu", "batch-cu", "FAQ__job80")
        ab.request_batch_cancel(80)
        ab._mark_batch(80, "abandoned")

        ab._save_batch(80, "tok-moi", "batch-moi", "FAQ__job80")

        self.assertFalse(ab._batch_cancel_requested(80))
        self.assertEqual(ab._get_batch(80)["batch_id"], "batch-moi")

    def test_migration_adds_column_to_an_older_table(self):
        # DB cũ tạo trước khi có cột -> _init_batches_table() phải ALTER thêm vào,
        # không được bỏ qua im lặng rồi vỡ lúc đọc cờ.
        with ab._connect() as con:
            con.execute("DROP TABLE akali_batches")
            con.execute("""CREATE TABLE akali_batches (
                akali_job_id INTEGER PRIMARY KEY, claim_token TEXT NOT NULL,
                batch_id TEXT NOT NULL, excel_stem TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')))""")

        ab._init_batches_table()

        ab._save_batch(80, "tok", "batch-80", "FAQ__job80")
        self.assertFalse(ab._batch_cancel_requested(80))
        ab.request_batch_cancel(80)
        self.assertTrue(ab._batch_cancel_requested(80))


class IdentifyFilesTests(unittest.TestCase):
    def test_empty_folder_raises_not_retryable(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ab.BridgeFailure) as ctx:
                ab._identify_files(Path(tmp))
            self.assertFalse(ctx.exception.retryable)

    def test_exactly_one_xlsx_and_one_video_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "a.xlsx").write_bytes(b"x")
            (folder / "video.mp4").write_bytes(b"x")
            xlsx, video = ab._identify_files(folder)
            self.assertEqual(xlsx.name, "a.xlsx")
            self.assertEqual(video.name, "video.mp4")

    def test_two_excel_files_raises_not_retryable(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "a.xlsx").write_bytes(b"x")
            (folder / "b.xlsx").write_bytes(b"x")
            (folder / "video.mp4").write_bytes(b"x")
            with self.assertRaises(ab.BridgeFailure) as ctx:
                ab._identify_files(folder)
            self.assertFalse(ctx.exception.retryable)


class FillVoicePlaceholdersTests(unittest.TestCase):
    def test_blank_cells_get_a_non_empty_placeholder_without_touching_filled_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            xlsx_path = Path(tmp) / "batch.xlsx"
            pd.DataFrame([
                {"product_name": "sp1", "subText": "hi", "video_type": "gioi_thieu",
                 "tts_provider": "", "tts_voice": None, "video_path": ""},
                {"product_name": "sp1", "subText": "hi2", "video_type": "gioi_thieu",
                 "tts_provider": "vbee", "tts_voice": "some_voice", "video_path": ""},
            ]).to_excel(xlsx_path, index=False)

            ab._fill_voice_placeholders(xlsx_path)

            reloaded = pd.read_excel(xlsx_path)
            self.assertTrue(str(reloaded.loc[0, "tts_provider"]).strip())
            self.assertTrue(str(reloaded.loc[0, "tts_voice"]).strip())
            self.assertEqual(reloaded.loc[1, "tts_provider"], "vbee")
            self.assertEqual(reloaded.loc[1, "tts_voice"], "some_voice")


class PrepareAndSubmitIntegrationTests(unittest.TestCase):
    """process_excel() thật (không mock) với default_videos + placeholder — đây là
    đúng cặp bước 6-8 của doc_api/APi_RENDER_QUEUE.md mục 5, chỉ dừng lại trước
    submit_jobs() (không đụng DB thật)."""

    def test_process_excel_accepts_blank_voice_columns_after_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            video_path = folder / "avatar_sp1.mp4"
            video_path.write_bytes(b"fake video bytes")

            xlsx_path = folder / "batch_from_akali.xlsx"
            pd.DataFrame([
                {"product_name": "sp1", "subText": "Xin chao", "video_type": "gioi_thieu",
                 "tts_provider": "", "tts_voice": None, "video_path": ""},
            ]).to_excel(xlsx_path, index=False)

            ab._fill_voice_placeholders(xlsx_path)
            ready_rows, errors = xi.process_excel(
                str(xlsx_path),
                default_videos=[{"name": video_path.stem, "path": str(video_path)}],
            )

            self.assertEqual(errors, [])
            self.assertEqual(len(ready_rows), 1)
            self.assertEqual(ready_rows[0]["video_path"], str(video_path))

            params = {"tts_provider": "autovoice", "voice_id": "pv-real-voice-123"}
            for r in ready_rows:
                r["tts_provider"] = params["tts_provider"]
                r["tts_voice"] = params["voice_id"]
            self.assertEqual(ready_rows[0]["tts_provider"], "autovoice")
            self.assertEqual(ready_rows[0]["tts_voice"], "pv-real-voice-123")


if __name__ == "__main__":
    unittest.main()
