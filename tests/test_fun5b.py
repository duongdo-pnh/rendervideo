"""Fun-5B engine wiring: DB rows, the worker's command line, the adapter and the API.

The engine itself (fun5b/, engines/Fun5B/.venv) is not exercised here — it needs the GPU and
~54GB of models; see docs/FUN5B_INTEGRATION_PLAN.md for the manual end-to-end check.
"""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

import database as db
import fun5b_adapter
import job_files
import queue_worker
import tts_db
from fun5b import plate_key


class _TempDb(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        for target, attr, value in ((db, "DB_PATH", self.tmp / "jobs.db"), (tts_db, "DB_PATH", self.tmp / "jobs.db"),
                                    (job_files, "UPLOADS_DIR", self.tmp / "uploads")):
            patcher = patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        db.init_db()
        tts_db.init_db()


class Fun5bJobRowTests(_TempDb):
    def test_add_fun5b_job_stores_its_options_and_neutral_lipsync_columns(self):
        job_id = db.add_fun5b_job("a", "/x/image.png", "/x/video.mp4", preset="fast", seed=7,
                                  keep_background=False, auto_background=False, table_line=0.8,
                                  sharp_hands=True, background_path="/x/background.png")
        row = db.get_job(job_id)
        self.assertEqual(row["engine"], "fun5b")
        self.assertEqual((row["ref_image_path"], row["video_path"], row["audio_path"]),
                         ("/x/image.png", "/x/video.mp4", ""))
        self.assertEqual((row["fun5b_preset"], row["seed"], row["keep_background"], row["auto_background"],
                          row["table_line"], row["sharp_hands"], row["background_path"]),
                         ("fast", 7, 0, 0, 0.8, 1, "/x/background.png"))

    def test_rejects_unknown_preset_and_out_of_range_table_line(self):
        with self.assertRaises(ValueError):
            db.add_fun5b_job("a", "i.png", "v.mp4", preset="Tự nhiên")
        with self.assertRaises(ValueError):
            db.add_fun5b_job("a", "i.png", "v.mp4", table_line=1.0)

    def test_add_job_refuses_fun5b(self):
        # The Excel/TTS path calls add_job(**render_config): a fun5b engine there must fail loudly.
        with self.assertRaises(ValueError):
            db.add_job("a", "v.mp4", "a.wav", "512", engine="fun5b")

    def test_progress_is_cleared_on_claim_and_retry_and_full_on_done(self):
        job_id = db.add_fun5b_job("a", "i.png", "v.mp4")
        db.claim_next_job()
        db.set_progress(job_id, 0.4)
        self.assertAlmostEqual(db.get_job(job_id)["progress"], 0.4)
        db.requeue_for_retry(job_id, "boom")
        self.assertIsNone(db.get_job(job_id)["progress"])
        db.claim_next_job()
        db.set_progress(job_id, 1.7)
        self.assertEqual(db.get_job(job_id)["progress"], 1.0)
        db.mark_done(job_id, "/out.mp4")
        self.assertEqual(db.get_job(job_id)["progress"], 1.0)


class WorkerCommandTests(_TempDb):
    def test_command_carries_every_option(self):
        job_id = db.add_fun5b_job("a", "/u/image.png", "/u/video.mp4", preset="balanced", seed=3,
                                  table_line=0.75, background_path="/u/background.png", sharp_hands=True)
        cmd = queue_worker._fun5b_command(db.get_job(job_id), Path("/w/out.mp4"))
        args = dict(zip(cmd[2::2], cmd[3::2]))
        self.assertEqual(args["--engine"], "fun5b")
        self.assertEqual(args["--job-id"], str(job_id))
        self.assertEqual((args["--ref-image"], args["--video"], args["--output"]),
                         ("/u/image.png", "/u/video.mp4", "/w/out.mp4"))
        self.assertEqual((args["--preset"], args["--seed"], args["--table-line"], args["--sharp-hands"]),
                         ("balanced", "3", "0.75", "1"))
        self.assertEqual(args["--background"], "/u/background.png")

    def test_automatic_table_line_and_no_background_are_left_out(self):
        job_id = db.add_fun5b_job("a", "/u/image.png", "/u/video.mp4")
        cmd = queue_worker._fun5b_command(db.get_job(job_id), Path("/w/out.mp4"))
        self.assertNotIn("--table-line", cmd)
        self.assertNotIn("--background", cmd)

    def test_rejected_render_fails_without_retry(self):
        job_id = db.add_fun5b_job("a", "/u/image.png", "/u/video.mp4")
        job = db.claim_next_job()
        with patch.object(queue_worker, "_run_render", return_value=queue_worker.RENDER_EXIT_REJECTED), \
                patch.object(queue_worker, "WORK_DIR", self.tmp / "work"), \
                patch.object(queue_worker, "LOGS_DIR", self.tmp):
            (self.tmp / f"job_{job_id}.log").write_text("[render_job] REJECTED: Không tìm thấy người\n")
            queue_worker._process_claimed_job(job)
        row = db.get_job(job_id)
        self.assertEqual(row["status"], db.STATUS_FAILED)
        self.assertEqual(row["retries"], 0)
        self.assertIn("Không tìm thấy người", row["error"])


class RenderJobCliTests(unittest.TestCase):
    def test_fun5b_engine_goes_to_its_own_cli(self):
        import render_job

        argv = ["render_job.py", "--engine", "fun5b", "--job-id", "5", "--ref-image", "i.png",
                "--video", "v.mp4", "--output", "o.mp4", "--table-line", "0.5"]
        with patch("sys.argv", argv), patch.object(render_job, "render_fun5b") as render:
            render_job.main()
        args = render.call_args.args
        self.assertEqual(args[:4], (5, "i.png", "v.mp4", "o.mp4"))
        self.assertEqual(args[8], 0.5)  # table_line

    def test_rejected_input_exits_with_the_no_retry_code(self):
        import render_job

        argv = ["render_job.py", "--engine", "fun5b", "--job-id", "5", "--ref-image", "i.png",
                "--video", "v.mp4", "--output", "o.mp4"]
        with patch("sys.argv", argv), \
                patch.object(render_job, "render_fun5b", side_effect=fun5b_adapter.RenderRejected("no person")), \
                self.assertRaises(SystemExit) as exit_:
            render_job.main()
        self.assertEqual(exit_.exception.code, render_job.EXIT_REJECTED)
        self.assertEqual(render_job.EXIT_REJECTED, queue_worker.RENDER_EXIT_REJECTED)


class ServerChunkMathTests(unittest.TestCase):
    def test_chunk_count_matches_fun5b_render(self):
        # fun5b_render_server._chunks needs torch; re-derive here from the same constants.
        chunk, known = 121, 13

        def chunks(generated):
            return 1 if generated <= chunk else 1 + -(-(generated - chunk) // (chunk - known))

        self.assertEqual(chunks(111), 1)   # 3s clip: 90 written + 21 lead-in frames
        self.assertEqual(chunks(121), 1)
        self.assertEqual(chunks(122), 2)
        self.assertEqual(chunks(922), 9)   # 30s clip: 901 + 21
        src = (Path(__file__).resolve().parent.parent / "fun5b_render_server.py").read_text()
        self.assertIn("1 if generated <= CHUNK else 1 + math.ceil((generated - CHUNK) / (CHUNK - KNOWN))", src)


class AdapterTests(unittest.TestCase):
    def test_render_refuses_when_not_installed(self):
        with patch.object(fun5b_adapter, "missing_files", return_value=["engines/Fun5B/.venv/bin/python"]):
            with self.assertRaises(fun5b_adapter.RenderRejected):
                fun5b_adapter.render(1, "i.png", "v.mp4", "o.mp4")

    def test_error_events_map_to_exceptions(self):
        cases = [({"event": "error", "message": "x", "cancelled": True}, fun5b_adapter.RenderCancelled),
                 ({"event": "error", "message": "no person", "retryable": False}, fun5b_adapter.RenderRejected),
                 ({"event": "error", "message": "boom", "retryable": True}, RuntimeError)]
        for event, exc in cases:
            with self.subTest(event=event), tempfile.TemporaryDirectory() as tmp, \
                    patch.object(fun5b_adapter, "missing_files", return_value=[]), \
                    patch.object(fun5b_adapter, "ACTIVE_LOCK", Path(tmp) / "a.lock"), \
                    patch.object(fun5b_adapter, "_ensure_server"), \
                    patch.object(fun5b_adapter, "_connect"), \
                    patch.object(fun5b_adapter, "_read_events", return_value=event):
                with self.assertRaises(exc):
                    fun5b_adapter.render(1, "i.png", "v.mp4", str(Path(tmp) / "o.mp4"))

    def test_ensure_stopped_is_a_noop_without_a_server(self):
        with patch.object(fun5b_adapter, "_server_pids", return_value=[]), \
                patch.object(fun5b_adapter, "_kill_server") as kill:
            fun5b_adapter.ensure_stopped()
        kill.assert_not_called()


def _png(size=(400, 700), colour=(200, 30, 30)):
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, "PNG")
    return buf.getvalue()


class ApiTests(_TempDb):
    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient

        import dashboard_reporter
        from backend import app as app_module, engines, jobs

        self.plates = self.tmp / "plates"
        self.plates.mkdir()
        for target, attr, value in ((dashboard_reporter, "dashboard_enabled", lambda: True),
                                    (jobs, "unavailable_reason", lambda: (None, [])),
                                    (engines, "unavailable_reason", lambda: (None, [])),
                                    (fun5b_adapter, "PLATE_DIR", self.plates)):
            patcher = patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(app_module.app)  # no `with`: the lifespan (worker start-up) never runs

    def _submit(self, **form):
        files = {"image": ("nv.png", _png(), "image/png"), "video": ("motion.mp4", b"fake-mp4", "video/mp4")}
        data = {"product": "ao", "kind": "VIDEO_DEMO", **form}
        return self.client.post("/api/jobs/animate", files=files, data=data)

    def test_animate_creates_a_queued_fun5b_job_with_its_uploads(self):
        r = self._submit(preset="fast", seed="9", table_line="80", sharp_hands="true")
        self.assertEqual(r.status_code, 201, r.text)
        row = db.get_job(r.json()["job_id"])
        self.assertEqual((row["engine"], row["status"], row["fun5b_preset"], row["seed"]),
                         ("fun5b", "queued", "fast", 9))
        self.assertAlmostEqual(row["table_line"], 0.8)
        self.assertTrue(Path(row["ref_image_path"]).is_file())
        self.assertEqual(Path(row["ref_image_path"]).parent, Path(row["video_path"]).parent)
        listed = self.client.get("/api/jobs").json()[0]
        self.assertEqual((listed["engine"], listed["fun5b_preset"], listed["sharp_hands"]), ("fun5b", "fast", True))
        self.assertEqual(self.client.get(f"/api/jobs/{row['id']}/ref-image").status_code, 200)

    def test_animate_without_naming_fields_is_named_after_its_files(self):
        r = self.client.post("/api/jobs/animate", files={"image": ("nv ao.png", _png(), "image/png"),
                                                         "video": ("mau 1.mp4", b"x", "video/mp4")})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual(r.json()["name"], "nv ao_mau 1")

    def test_animate_validates_inputs(self):
        self.assertEqual(self._submit(preset="nope").status_code, 422)
        self.assertEqual(self._submit(table_line="120").status_code, 422)
        bad = self.client.post("/api/jobs/animate", data={"kind": "VIDEO_DEMO"},
                               files={"image": ("a.gif", b"x", "image/gif"), "video": ("v.mp4", b"x", "video/mp4")})
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(db.list_jobs(), [])

    def test_animate_refused_when_engine_unavailable(self):
        from backend import jobs

        with patch.object(jobs, "unavailable_reason", lambda: ("Chưa cài Fun-5B", ["x"])):
            r = self._submit()
        self.assertEqual(r.status_code, 422)
        self.assertIn("Chưa cài", r.json()["detail"])

    def test_info_lists_presets(self):
        info = self.client.get("/api/engines/fun5b").json()
        self.assertTrue(info["available"])
        self.assertEqual([p["key"] for p in info["presets"]], list(db.FUN5B_PRESETS))
        self.assertEqual(info["defaults"]["preset"], "natural")

    def test_plates_list_lookup_and_delete(self):
        from PIL import ImageOps

        image = Image.open(io.BytesIO(_png()))
        fitted = np.asarray(ImageOps.fit(image.convert("RGB"), (640, 1152), Image.LANCZOS))
        key = plate_key.cache_key(fitted.tobytes())
        self.assertFalse(self.client.post("/api/engines/fun5b/plates/lookup",
                                          files={"image": ("a.png", _png(), "image/png")}).json()["cached"])
        Image.new("RGB", (640, 1152)).save(self.plates / f"{key}.png")
        (self.plates / f"{key}.json").write_text(json.dumps({"source_name": "nv.png", "last_used": "2026-10-30 10:00:00"}))
        lookup = self.client.post("/api/engines/fun5b/plates/lookup", files={"image": ("a.png", _png(), "image/png")})
        self.assertEqual(lookup.json(), {"id": key, "cached": True})
        plates = self.client.get("/api/engines/fun5b/plates").json()
        self.assertEqual([(p["id"], p["source_name"], p["has_source"]) for p in plates], [(key, "nv.png", False)])
        self.assertEqual(self.client.get(f"/api/engines/fun5b/plates/{key}/image").status_code, 200)
        self.assertEqual(self.client.get("/api/engines/fun5b/plates/..jobs/image").status_code, 404)
        self.assertEqual(self.client.delete("/api/engines/fun5b/plates/not-a-plate-id").status_code, 404)
        self.assertEqual(self.client.delete(f"/api/engines/fun5b/plates/{key}").status_code, 200)
        self.assertEqual(self.client.get("/api/engines/fun5b/plates").json(), [])

    def test_server_status_when_not_running(self):
        with patch.object(fun5b_adapter, "server_status", return_value=None):
            self.assertEqual(self.client.get("/api/engines/fun5b/server").json()["running"], False)


if __name__ == "__main__":
    unittest.main()
