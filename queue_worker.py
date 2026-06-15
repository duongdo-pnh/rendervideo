"""24/7 render worker.

Loop: GPU health check -> 6-hourly SQLite backup -> claim oldest queued job ->
normalize video (NVENC) + audio (16kHz mono) -> render in an isolated subprocess ->
copy result to downloads/ on success, else retry up to MAX_RETRIES then fail.

The render runs as a subprocess (render_job.py) so a crash there never kills this loop.
Run with:  conda activate latentsync && python queue_worker.py
"""
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import database as db

ROOT = Path(__file__).parent
DOWNLOADS_DIR = ROOT / "downloads"
WORK_DIR = ROOT / "work"
LOGS_DIR = ROOT / "logs"
BACKUP_DIR = ROOT / "backups"

POLL_SECONDS = 5            # idle poll interval when the queue is empty
BACKUP_INTERVAL = 6 * 3600  # SQLite backup cadence
BACKUP_KEEP = 8             # keep this many most-recent backups
GPU_MIN_FREE_MB = 2048      # require at least this much free VRAM before claiming a job
GPU_WAIT_SECONDS = 30       # back-off when the GPU is unhealthy/busy

_RUNNING = True


def _log(msg):
    print(f"[worker {datetime.now():%H:%M:%S}] {msg}", flush=True)


def _ensure_dirs():
    for d in (DOWNLOADS_DIR, WORK_DIR, LOGS_DIR, BACKUP_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- GPU health

def gpu_healthy():
    """True if nvidia-smi responds and free VRAM >= GPU_MIN_FREE_MB."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode != 0:
            _log(f"GPU check failed: {out.stderr.strip()}")
            return False
        free_mb = min(int(x) for x in out.stdout.split())  # min across GPUs
        if free_mb < GPU_MIN_FREE_MB:
            _log(f"GPU busy: {free_mb}MB free < {GPU_MIN_FREE_MB}MB needed")
            return False
        return True
    except Exception as e:
        _log(f"GPU check error: {e}")
        return False


# ---------------------------------------------------------------- normalize

def _ffprobe_duration(path):
    """Seconds of media at path, or 0.0 if unknown."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float(out.stdout.strip())
    except Exception:
        return 0.0


def normalize_video(src, work_dir):
    """Re-encode to 25fps via NVENC (GPU). Falls back to libx264 if NVENC fails."""
    dst = str(Path(work_dir) / "norm_video.mp4")
    nvenc = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
             "-r", "25", "-c:v", "h264_nvenc", "-preset", "p4", "-cq", "19",
             "-pix_fmt", "yuv420p", "-an", dst]
    r = subprocess.run(nvenc, capture_output=True, text=True)
    if r.returncode == 0:
        return dst
    _log(f"NVENC failed ({r.stderr.strip()[:200]}); falling back to libx264")
    x264 = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
            "-r", "25", "-c:v", "libx264", "-crf", "18", "-preset", "medium",
            "-pix_fmt", "yuv420p", "-an", dst]
    subprocess.run(x264, check=True)
    return dst


def normalize_audio(src, work_dir):
    """Resample to 16kHz mono WAV (what the pipeline's whisper/audio reader expects)."""
    dst = str(Path(work_dir) / "norm_audio.wav")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
         "-ar", "16000", "-ac", "1", dst],
        check=True,
    )
    return dst


def render_timeout(video_path):
    """Dynamic ceiling: max(1h, 10x video duration). 30-min video -> 5h."""
    dur = _ffprobe_duration(video_path)
    return int(max(3600, dur * 10))


# ---------------------------------------------------------------- backup

def backup_db():
    if not db.DB_PATH.exists():
        return
    import sqlite3
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = BACKUP_DIR / f"jobs_{ts}.db"
    src = sqlite3.connect(db.DB_PATH)
    try:
        out = sqlite3.connect(dst)            # online backup — safe while worker writes
        src.backup(out)
        out.close()
    finally:
        src.close()
    _log(f"DB backup -> {dst.name}")
    backups = sorted(BACKUP_DIR.glob("jobs_*.db"))
    for old in backups[:-BACKUP_KEEP]:        # prune oldest beyond BACKUP_KEEP
        old.unlink(missing_ok=True)


# ---------------------------------------------------------------- one job

def _safe_name(name):
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in (name or "job"))[:60] or "job"


def process_job(job):
    job_id = job["id"]
    _log(f"claim job #{job_id} '{job['name']}' model={job['model_res']}")
    work = WORK_DIR / str(job_id)
    work.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / f"job_{job_id}.log"

    nv = normalize_video(job["video_path"], work)
    na = normalize_audio(job["audio_path"], work)
    out_path = work / "out.mp4"
    timeout = render_timeout(nv)
    _log(f"job #{job_id} normalized; rendering (timeout {timeout//60}min) -> {log_path.name}")

    cmd = [
        sys.executable, str(ROOT / "render_job.py"),
        "--video", nv, "--audio", na, "--output", str(out_path),
        "--config", job["config_path"], "--checkpoint", job["checkpoint_path"],
        "--guidance", str(job["guidance"]), "--steps", str(job["steps"]),
        "--seed", str(job["seed"]), "--enhance_mouth", str(job["enhance_mouth"]),
        "--enhance_region", job["enhance_region"], "--out_res", job["out_res"],
    ]
    with open(log_path, "w") as lf:
        proc = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT, timeout=timeout)

    if proc.returncode != 0 or not out_path.exists():
        tail = _tail(log_path)
        raise RuntimeError(f"render rc={proc.returncode}; log tail:\n{tail}")

    # Point 4: download ra ĐÚNG TÊN nhập (tên này = khóa match khi import sang hệ live).
    dst = DOWNLOADS_DIR / f"{_safe_name(job['name'])}.mp4"
    if dst.exists():
        dst = DOWNLOADS_DIR / f"{_safe_name(job['name'])}_{job_id}.mp4"
    shutil.copy(out_path, dst)
    db.mark_done(job_id, str(dst))
    _log(f"job #{job_id} DONE -> {dst}")


def _tail(path, n=15):
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-n:])
    except Exception:
        return "(no log)"


# ---------------------------------------------------------------- main loop

def _stop(*_):
    global _RUNNING
    _RUNNING = False
    _log("shutdown signal received; finishing current cycle...")


def main():
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    db.init_db()
    _ensure_dirs()
    n = db.reset_stuck_jobs()
    if n:
        _log(f"requeued {n} stuck 'rendering' job(s) from a previous run")
    _log("worker started; polling for jobs...")

    last_backup = time.time()
    while _RUNNING:
        try:
            if not gpu_healthy():
                time.sleep(GPU_WAIT_SECONDS)
                continue

            if time.time() - last_backup >= BACKUP_INTERVAL:
                backup_db()
                last_backup = time.time()

            job = db.claim_next_job()
            if not job:
                time.sleep(POLL_SECONDS)
                continue

            try:
                process_job(job)
            except Exception as e:
                if job["retries"] < db.MAX_RETRIES:
                    db.requeue_for_retry(job["id"], e)
                    _log(f"job #{job['id']} failed (retry {job['retries']+1}/{db.MAX_RETRIES}): {e}")
                else:
                    db.mark_failed(job["id"], e)
                    _log(f"job #{job['id']} FAILED permanently: {e}")
        except Exception as loop_err:
            # Never let the loop die — log and keep going.
            _log(f"loop error (continuing): {loop_err}")
            time.sleep(POLL_SECONDS)

    _log("worker stopped.")


if __name__ == "__main__":
    main()
