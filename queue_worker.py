"""24/7 render worker.

Loop: GPU health check -> 6-hourly SQLite backup -> claim oldest queued job ->
normalize video (NVENC) + audio (16kHz mono) -> render in an isolated subprocess ->
copy result to downloads/ on success, else retry up to MAX_RETRIES then fail.

The render runs as a subprocess (render_job.py) so a crash there never kills this loop.
Run with:  conda activate latentsync && python queue_worker.py
"""
import os
import shutil
import signal
import subprocess
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path

# Thêm bin của python hiện hành (conda env) vào PATH -> subprocess thấy ffmpeg/ffprobe
# (chạy 'python queue_worker.py' trực tiếp không activate env nên PATH thiếu bin của env).
_envbin = os.path.dirname(sys.executable)
if _envbin and _envbin not in os.environ.get("PATH", "").split(os.pathsep):
    os.environ["PATH"] = _envbin + os.pathsep + os.environ.get("PATH", "")

import database as db

ROOT = Path(__file__).parent
DOWNLOADS_DIR = db.RENDERS_DIR        # video render xong tự đổ ra Desktop cho dễ nhìn
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


def render_timeout(video_path, audio_path=None):
    """Dynamic ceiling. The renderer extends the carrier to AUDIO length and (with mouth
    enhancement) runs GFPGAN per frame, so the work scales with the LONGER of video/audio,
    not the (possibly shorter, pre-extension) normalized video. Budget ~12x realtime plus a
    fixed ~20min headroom for model load + scenedetect + normalize. 30-min clip -> ~6.3h."""
    dur = _ffprobe_duration(video_path)
    if audio_path:
        dur = max(dur, _ffprobe_duration(audio_path))
    return int(max(3600, dur * 12) + 1200)


def trim_video_to_audio(video_path, audio_path, work_dir, margin=2.0):
    """If the carrier video is LONGER than the audio, stream-copy trim it to ~audio length BEFORE
    NVENC normalize, so we never re-encode (here + downscale + scenedetect downstream) the tail the
    renderer discards anyway — it only renders audio-length. Near-instant (no re-encode).

    No-op if the video is already <= audio+margin: short carriers are forward-looped + cut to audio
    length downstream by prepare_carrier. +margin keeps it safely longer than the audio (stream-copy
    cuts on keyframe boundaries). Falls back to the original on any failure (correctness first)."""
    vdur = _ffprobe_duration(video_path)
    adur = _ffprobe_duration(audio_path)
    if not vdur or not adur or vdur <= adur + margin:
        return str(video_path)
    keep = adur + margin
    out = str(Path(work_dir) / "trim_video.mp4")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-nostdin", "-i", str(video_path),
             "-t", f"{keep:.3f}", "-c", "copy", "-an", out],
            check=True,
        )
    except Exception as e:
        _log(f"trim failed ({e}); using full video")
        return str(video_path)
    if _ffprobe_duration(out) < adur:            # keyframe cut landed too short -> unsafe, skip
        return str(video_path)
    _log(f"trim carrier {vdur:.0f}s -> ~{keep:.0f}s (audio {adur:.0f}s) before normalize")
    return out


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
    # NFC: gộp dấu tổ hợp tiếng Việt (NFD) -> ký tự dựng sẵn, để isalnum() GIỮ được chữ có dấu
    # (không thì 'ả' = 'a'+dấu rời, dấu bị thay '_' -> tên nát kiểu 'Cha_o_chô_ng').
    name = unicodedata.normalize("NFC", name or "job")
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:60] or "job"


def process_job(job):
    job_id = job["id"]
    _log(f"claim job #{job_id} '{job['name']}' model={job['model_res']}")
    work = WORK_DIR / str(job_id)
    work.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / f"job_{job_id}.log"

    # Trim a too-long carrier to ~audio length first (cheap) so NVENC normalize + the renderer's
    # downscale/scenedetect don't churn through video that gets discarded. Short videos pass through.
    src_video = trim_video_to_audio(job["video_path"], job["audio_path"], work)
    nv = normalize_video(src_video, work)
    na = normalize_audio(job["audio_path"], work)
    out_path = work / "out.mp4"
    timeout = render_timeout(nv, na)
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
