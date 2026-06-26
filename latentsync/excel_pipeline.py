"""ExcelPipeline — batch pipeline: Excel → TTS → render queue → Google Drive → Excel.

Flow per row (sequential, single GPU):
  1. Update status → "rendering"
  2. TTS: text → audio file
  3. Submit to render queue (queue_worker.py renders nền)
  4. Poll DB mỗi 10s chờ job xong
  5. Upload kết quả lên Google Drive (nếu bật)
  6. Ghi video_done, video_url, status, render_time_s ngược vào Excel

Usage (CLI):
    python -m latentsync.excel_pipeline batch.xlsx --drive

Usage (Python):
    from latentsync.excel_pipeline import ExcelPipeline
    for line in ExcelPipeline("batch.xlsx", upload_drive=True).run():
        print(line)
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import database as db
from latentsync.tts.factory import synthesize
from latentsync.excel_import import ExcelImporter

TTS_AUDIO_DIR = ROOT / "temp"
DOWNLOADS_DIR = ROOT / "downloads"

RENDER_DEFAULTS = dict(
    model_res="256",
    guidance=1.5,
    steps=20,
    seed=1247,
    enhance_mouth=1,
    enhance_region="mouth",
    out_res="720",
)


# ------------------------------------------------------------------ naming

def make_output_name(item_id: str, vtype: str) -> str:
    """Tên file output tránh trùng.

    {item_id}-{vtype}.mp4 → nếu trùng → {item_id}-{vtype}-1.mp4 → -2 …
    item_id rỗng → 'general-{vtype}'.
    """
    item_id = (item_id or "general").strip()
    vtype = (vtype or "gioi_thieu").strip()
    base = f"{item_id}-{vtype}"
    if not (DOWNLOADS_DIR / f"{base}.mp4").exists():
        return f"{base}.mp4"
    n = 1
    while (DOWNLOADS_DIR / f"{base}-{n}.mp4").exists():
        n += 1
    return f"{base}-{n}.mp4"


# ------------------------------------------------------------------ wait

def wait_for_job(job_id: int, timeout: int = 3600, poll: int = 10) -> dict:
    """Poll DB every `poll` seconds. Return completed job dict or raise."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = db.get_job(job_id)
        if job is None:
            raise RuntimeError(f"Job #{job_id} không tồn tại trong DB.")
        if job["status"] == db.STATUS_DONE:
            return job
        if job["status"] == db.STATUS_FAILED:
            raise RuntimeError(f"Job #{job_id} thất bại: {job.get('error', '')}")
        time.sleep(poll)
    raise TimeoutError(f"Job #{job_id} vẫn chưa xong sau {timeout}s.")


# ------------------------------------------------------------------ pipeline

class ExcelPipeline:
    def __init__(
        self,
        excel_path: str,
        default_video: str = None,
        default_tts_provider: str = None,
        upload_drive: bool = False,
        gdrive_secret: str = "client_secret.json",
        gdrive_token: str = "token.json",
        gdrive_folder: str = "ReliveStudio_Videos",
    ):
        self.excel_path = str(excel_path)
        self.default_video = default_video
        self.default_tts_provider = default_tts_provider
        self.upload_drive = upload_drive

        db.init_db()
        TTS_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

        self.importer = ExcelImporter(self.excel_path)
        self.uploader = None
        if upload_drive:
            from latentsync.gdrive_upload import GDriveUploader
            self.uploader = GDriveUploader(
                secret_path=gdrive_secret,
                token_path=gdrive_token,
                folder_name=gdrive_folder,
            )

    def run(self):
        """Generator: yield log strings. Process pending rows sequentially."""
        self.importer.reload()
        pending = self.importer.get_pending_rows()
        if not pending:
            yield "ℹ️ Không có row nào ở trạng thái pending."
            return

        total = len(pending)
        yield f"▶️ Bắt đầu xử lý {total} row..."

        for idx, row in enumerate(pending, 1):
            row_num = row["_row"]
            label = row.get("product_name") or row.get("shopee_item_id") or f"row {row_num}"
            yield f"\n━━ [{idx}/{total}] Row {row_num}: {label} ━━"
            yield from self._process_row(row, row_num)

        yield "\n✅ Pipeline hoàn tất."

    def _process_row(self, row, row_num):
        start = time.time()
        try:
            # -- status → rendering
            self.importer.update_row(row_num, status="rendering")
            yield "  → status = rendering"

            # -- resolve video path
            video_path = row["video_path"] or self.default_video
            if not video_path:
                raise ValueError("Không có video_path và chưa đặt video mặc định.")
            if not os.path.exists(video_path):
                raise ValueError(f"video_path không tồn tại: {video_path!r}")

            # -- TTS
            provider = row["tts_provider"] or self.default_tts_provider or None
            voice = row["tts_voice"] or None
            audio_name = f"tts_{row_num}_{int(time.time())}.wav"
            audio_path = str(TTS_AUDIO_DIR / audio_name)
            text_preview = (row["text"] or "")[:50]
            yield f"  → TTS ({provider or 'default'}): {text_preview!r}"
            synthesize(text=row["text"], output_path=audio_path,
                       provider=provider, voice=voice)
            if not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
                raise RuntimeError("TTS không tạo được file audio.")
            yield f"  → audio: {audio_name}"

            # -- output naming & job submission
            item_id = (row["shopee_item_id"] or "general").strip()
            vtype = (row["video_type"] or "gioi_thieu").strip()
            out_name = make_output_name(item_id, vtype)
            job_name = out_name.replace(".mp4", "")   # worker appends .mp4

            job_id = db.add_job(job_name, video_path, audio_path, **RENDER_DEFAULTS)
            yield f"  → Job #{job_id} vào hàng đợi → {out_name}"

            # -- wait
            yield "  → Chờ render (timeout 1h)..."
            done_job = wait_for_job(job_id, timeout=3600)
            out_path = done_job.get("output_path") or ""
            yield f"  → Render xong: {Path(out_path).name if out_path else '?'}"

            # -- Google Drive upload
            drive_url = ""
            if self.upload_drive and self.uploader and out_path and os.path.exists(out_path):
                yield "  → Upload Google Drive..."
                drive_url = self.uploader.upload(out_path)
                yield f"  → Drive URL: {drive_url[:60]}..."

            # -- update Excel
            elapsed = int(time.time() - start)
            self.importer.update_row(
                row_num,
                video_done=out_path,
                video_url=drive_url,
                status="done",
                render_time_s=elapsed,
            )
            yield f"  ✅ Done — {elapsed}s"

        except Exception as e:
            elapsed = int(time.time() - start)
            try:
                self.importer.update_row(
                    row_num,
                    status="error",
                    note=str(e)[:500],
                    render_time_s=elapsed,
                )
            except Exception:
                pass
            yield f"  ❌ Lỗi: {e}"


# ------------------------------------------------------------------ CLI

if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Chạy ExcelPipeline từ terminal.")
    ap.add_argument("excel", help="Đường dẫn file Excel (.xlsx)")
    ap.add_argument("--video", default=None, help="Video mặc định (nếu cột video_path trống)")
    ap.add_argument("--provider", default=None, help="TTS provider mặc định")
    ap.add_argument("--drive", action="store_true", help="Upload Google Drive sau render")
    args = ap.parse_args()

    pipeline = ExcelPipeline(
        excel_path=args.excel,
        default_video=args.video,
        default_tts_provider=args.provider,
        upload_drive=args.drive,
    )
    for line in pipeline.run():
        print(line, flush=True)
