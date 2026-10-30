"""Shared upload-storage helpers for the render queue.

Gradio's web_ui.py and the FastAPI backend both need jobs to land in the exact
same place on disk (queue_worker.py reads paths straight out of the DB, it
doesn't care which UI created the job) and to clean up the same way.
"""
import shutil
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UPLOADS_DIR = ROOT / "uploads"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


def save_upload_pair(video_src, audio_src):
    """Copy an uploaded video+audio pair into a stable per-job directory.

    Gradio's own upload temp files get cleaned up when the request ends, and a
    browser file upload only exists as request-scoped bytes — either way the
    render queue needs its own durable copy. Returns (video_dst, audio_dst).
    """
    job_dir = UPLOADS_DIR / uuid.uuid4().hex[:12]
    job_dir.mkdir(parents=True, exist_ok=True)
    video_dst = job_dir / ("video" + Path(video_src).suffix)
    audio_dst = job_dir / ("audio" + Path(audio_src).suffix)
    shutil.copy(video_src, video_dst)
    shutil.copy(audio_src, audio_dst)
    return video_dst, audio_dst


def write_upload_pair(video_name, video_bytes, audio_name, audio_bytes):
    """Same as save_upload_pair, but for in-memory bytes (FastAPI UploadFile.read())."""
    job_dir = UPLOADS_DIR / uuid.uuid4().hex[:12]
    job_dir.mkdir(parents=True, exist_ok=True)
    video_dst = job_dir / ("video" + Path(video_name).suffix)
    audio_dst = job_dir / ("audio" + Path(audio_name).suffix)
    video_dst.write_bytes(video_bytes)
    audio_dst.write_bytes(audio_bytes)
    return video_dst, audio_dst


def write_fun5b_upload(image_name, image_bytes, video_name, video_bytes, background_name=None,
                       background_bytes=None):
    """A Fun-5B job's uploads (character image, driving video, optional clean plate) in one
    uploads/<hex>/ dir, so cleanup_job_files() (keyed on video_path) removes all of them.
    Returns (image_dst, video_dst, background_dst or None)."""
    job_dir = UPLOADS_DIR / uuid.uuid4().hex[:12]
    job_dir.mkdir(parents=True, exist_ok=True)
    image_dst = job_dir / ("image" + (Path(image_name).suffix or ".png"))
    video_dst = job_dir / ("video" + (Path(video_name).suffix or ".mp4"))
    image_dst.write_bytes(image_bytes)
    video_dst.write_bytes(video_bytes)
    background_dst = None
    if background_bytes:
        background_dst = job_dir / ("background" + (Path(background_name or "").suffix or ".png"))
        background_dst.write_bytes(background_bytes)
    return image_dst, video_dst, background_dst


def stabilize_video(path):
    """Move a default/template video for an Excel batch into a durable location
    (uploads/videos/) so it survives the request that uploaded it."""
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return str(p)
    vids = UPLOADS_DIR / "videos"
    vids.mkdir(parents=True, exist_ok=True)
    if vids in p.parents:  # đã nằm trong kho bền rồi
        return str(p)
    dst = vids / (uuid.uuid4().hex[:12] + (p.suffix or ".mp4"))
    shutil.copy(p, dst)
    return str(dst)


def write_default_video(filename, file_bytes):
    """Same idea as stabilize_video, but writes in-memory bytes (FastAPI UploadFile.read())
    straight to a stable unique path — no intermediate temp file. (Writing a temp file
    inside uploads/videos/ first and then calling stabilize_video() on it is a trap: that
    function treats "already inside uploads/videos/" as "already stable" and returns the
    path as-is instead of copying, so a caller that then deletes its own temp file deletes
    the only copy.)"""
    vids = UPLOADS_DIR / "videos"
    vids.mkdir(parents=True, exist_ok=True)
    suffix = Path(filename or "").suffix or ".mp4"
    dst = vids / (uuid.uuid4().hex[:12] + suffix)
    dst.write_bytes(file_bytes)
    return str(dst)


def stabilize_excel(path):
    """Copy an uploaded Excel batch file into a durable location (uploads/imports/),
    keeping its ORIGINAL basename — that basename becomes the batch's Drive subfolder
    name later, so it must not be replaced with a uuid."""
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return str(p)
    d = UPLOADS_DIR / "imports" / uuid.uuid4().hex[:12]
    d.mkdir(parents=True, exist_ok=True)
    dst = d / (p.name or "import.xlsx")
    shutil.copy(p, dst)
    return str(dst)


def write_excel_upload(filename, file_bytes):
    """Same as stabilize_excel, but for in-memory bytes (FastAPI UploadFile.read())."""
    d = UPLOADS_DIR / "imports" / uuid.uuid4().hex[:12]
    d.mkdir(parents=True, exist_ok=True)
    dst = d / (filename or "import.xlsx")
    dst.write_bytes(file_bytes)
    return str(dst)


def cleanup_job_files(row):
    """Best-effort: dọn file input đã upload của job (GIỮ lại video kết quả trên Desktop)."""
    try:
        vp = Path(row.get("video_path") or "")
        if (
            vp.exists()
            and UPLOADS_DIR in vp.parents
            and vp.parent.parent == UPLOADS_DIR
            and vp.parent.name not in {"videos", "tts"}
        ):   # uploads/<hex>/video.ext -> xóa cả thư mục tạm; không xóa uploads/videos hoặc uploads/tts
            shutil.rmtree(vp.parent, ignore_errors=True)
    except Exception:
        pass
