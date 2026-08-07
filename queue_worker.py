"""24/7 render worker.

Loop: GPU health check -> 6-hourly SQLite backup -> claim oldest queued job ->
normalize video (NVENC) + audio (16kHz mono) -> render in an isolated subprocess ->
copy result to downloads/ on success, else retry up to MAX_RETRIES then fail.

The render runs as a subprocess (render_job.py) so a crash there never kills this loop.
Run with:  conda activate latentsync && python queue_worker.py
"""
import fcntl
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# Thêm bin của python hiện hành (conda env) vào PATH -> subprocess thấy ffmpeg/ffprobe
# (chạy 'python queue_worker.py' trực tiếp không activate env nên PATH thiếu bin của env).
_envbin = os.path.dirname(sys.executable)
if _envbin and _envbin not in os.environ.get("PATH", "").split(os.pathsep):
    os.environ["PATH"] = _envbin + os.pathsep + os.environ.get("PATH", "")

import database as db

# Google Drive auto-upload là tùy chọn: thiếu thư viện google-* thì worker vẫn chạy bình thường.
try:
    import google_drive_upload as gdrive
except Exception as _gdrive_err:
    gdrive = None
    _GDRIVE_IMPORT_ERROR = _gdrive_err

ROOT = Path(__file__).parent
DOWNLOADS_DIR = db.RENDERS_DIR        # video render xong tự đổ ra Desktop cho dễ nhìn
WORK_DIR = ROOT / "work"
LOGS_DIR = ROOT / "logs"
BACKUP_DIR = ROOT / "backups"
WORKER_LOCK_PATH = ROOT / ".queue_worker.lock"

POLL_SECONDS = 5            # idle poll interval when the queue is empty
BACKUP_INTERVAL = 6 * 3600  # SQLite backup cadence
BACKUP_KEEP = 8             # keep this many most-recent backups
GPU_MIN_FREE_MB = 2048      # require at least this much free VRAM before claiming a job
GPU_WAIT_SECONDS = 30       # back-off when the GPU is unhealthy/busy
RENDER_IDLE_TIMEOUT = 20 * 60  # no new log output this long means the renderer is genuinely stuck
RENDER_POLL_SECONDS = 5
MUSETALK_BATCH_COLLECT_SECONDS = 10
MUSETALK_BATCH_POLL_SECONDS = 0.5

_RUNNING = True
_WORKER_LOCK_FILE = None


def _log(msg):
    print(f"[worker {datetime.now():%H:%M:%S}] {msg}", flush=True)


def _ensure_dirs():
    for d in (DOWNLOADS_DIR, WORK_DIR, LOGS_DIR, BACKUP_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _acquire_worker_lock():
    """Allow exactly one queue worker for this project.

    SQLite makes claiming a job atomic, but it intentionally does not limit the number of
    consumers. Multiple workers therefore claim different jobs and launch render subprocesses
    that wait on the same GPU lock. That wait used to count against each render timeout, which is
    especially harmful for slower 512 jobs. Keep the lock file descriptor open for the lifetime
    of this process; the OS releases the lock automatically if the worker exits or crashes.
    """
    global _WORKER_LOCK_FILE
    lock_file = open(WORKER_LOCK_PATH, "a+")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.seek(0)
        owner = lock_file.read().strip() or "unknown"
        lock_file.close()
        _log(f"another queue worker is already running (pid={owner}); exiting")
        return False

    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    _WORKER_LOCK_FILE = lock_file
    return True


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


def normalize_video(src, work_dir, out_res=None):
    """Re-encode to 25fps via NVENC (GPU). Falls back to libx264 if NVENC fails."""
    dst = str(Path(work_dir) / "norm_video.mp4")
    target = {"1080": 1080, "720": 720, "480 Nhanh": 480}.get(out_res)
    vf = []
    if target:
        vf.append(
            f"scale=\x27if(gt(iw,ih),-2,min(iw,{target}))\x27:"
            f"\x27if(gt(iw,ih),min(ih,{target}),-2)\x27"
        )
    vf.append("fps=25")
    video_filter = ",".join(vf)
    nvenc = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
             "-vf", video_filter, "-c:v", "h264_nvenc", "-preset", "p1",
             "-tune", "ll", "-cq", "19",
             "-pix_fmt", "yuv420p", "-an", dst]
    r = subprocess.run(nvenc, capture_output=True, text=True)
    if r.returncode == 0:
        return dst
    _log(f"NVENC failed ({r.stderr.strip()[:200]}); falling back to libx264")
    x264 = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
            "-vf", video_filter, "-c:v", "libx264", "-crf", "18", "-preset", "veryfast",
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


def _stop_process_tree(proc):
    """Stop the render process group, including ffmpeg children, without leaving GPU users."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
    except ProcessLookupError:
        pass


def _run_render(cmd, log_path, idle_timeout=RENDER_IDLE_TIMEOUT):
    """Run a render while distinguishing slow-but-progressing work from a real stall."""
    last_progress = time.monotonic()
    last_size = -1

    with open(log_path, "w") as lf:
        proc = subprocess.Popen(
            cmd,
            stdout=lf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            while True:
                returncode = proc.poll()
                if returncode is not None:
                    return returncode

                now = time.monotonic()
                try:
                    size = log_path.stat().st_size
                except OSError:
                    size = 0
                if size != last_size:
                    last_size = size
                    last_progress = now

                if now - last_progress > idle_timeout:
                    raise RuntimeError(
                        f"render stalled: no log progress for {idle_timeout // 60} minutes"
                    )
                time.sleep(RENDER_POLL_SECONDS)
        except BaseException:
            _stop_process_tree(proc)
            raise


def trim_video_for_musetalk(video_path, work_dir, seconds=30):
    """Create a stable, audio-independent avatar carrier for MuseTalk caching."""
    if _ffprobe_duration(video_path) <= seconds:
        return str(video_path)
    out = str(Path(work_dir) / "musetalk_carrier.mp4")
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-nostdin", "-i", str(video_path),
                        "-t", str(seconds), "-c", "copy", "-an", out], check=True)
        return out
    except Exception as e:
        _log(f"MuseTalk fixed carrier trim failed ({e}); using full video")
        return str(video_path)


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
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    if len(safe) <= 140:
        return safe or "job"
    if "__ASK_" in safe:
        head, tail = safe.split("__ASK_", 1)
        suffix = f"__ASK_{tail}"
        keep_head = max(20, 140 - len(suffix))
        return f"{head[:keep_head]}{suffix}"[:140] or "job"
    if "__" in safe:
        head, tail = safe.rsplit("__", 1)
        keep_head = max(20, 140 - len(tail) - 2)
        return f"{head[:keep_head]}__{tail}"[:140] or "job"
    return safe[:140] or "job"


def _drive_safe_name(path):
    """ASCII-only Drive filename with one underscore between name segments."""
    path = Path(path)
    ascii_stem = unicodedata.normalize("NFKD", path.stem).encode(
        "ascii", "ignore"
    ).decode("ascii")
    stem = re.sub(r"[^A-Za-z0-9]+", "_", ascii_stem).strip("_") or "video"
    suffix = re.sub(r"[^A-Za-z0-9]", "", path.suffix.lstrip("."))
    return f"{stem[:140]}.{suffix}" if suffix else stem[:140]


def process_job(job):
    job_id = job["id"]
    _log(f"claim job #{job_id} '{job['name']}' model={job['model_res']}")
    work = WORK_DIR / str(job_id)
    work.mkdir(parents=True, exist_ok=True)
    log_path = LOGS_DIR / f"job_{job_id}.log"

    # Keep MuseTalk video stable across different audio lengths so its avatar cache is reusable.
    if job.get("engine", "musetalk") == "musetalk":
        src_video = trim_video_for_musetalk(job["video_path"], work)
    else:
        src_video = trim_video_to_audio(job["video_path"], job["audio_path"], work)
    nv = normalize_video(src_video, work, job["out_res"])
    na = normalize_audio(job["audio_path"], work)
    out_path = work / "out.mp4"
    # Do not impose a wall-clock limit. Long 512 renders can legitimately take many hours;
    # killing a healthy subprocess because it crossed an estimate loses all completed work.
    # A stalled subprocess is still stopped if it stops writing logs for too long.
    _log(
        f"job #{job_id} normalized; rendering "
        f"(no time limit, stall {RENDER_IDLE_TIMEOUT//60}min) -> {log_path.name}"
    )

    cmd = [
        sys.executable, str(ROOT / "render_job.py"),
        "--video", nv, "--audio", na, "--output", str(out_path),
        "--config", job["config_path"], "--checkpoint", job["checkpoint_path"],
        "--guidance", str(job["guidance"]), "--steps", str(job["steps"]),
        "--seed", str(job["seed"]), "--enhance_mouth", str(job["enhance_mouth"]),
        "--enhance_region", job["enhance_region"], "--out_res", job["out_res"],
        "--input_type", job.get("input_type", "real"),
        "--engine", job.get("engine", "musetalk"),
    ]
    returncode = _run_render(cmd, log_path)

    if returncode != 0 or not out_path.exists():
        tail = _tail(log_path)
        raise RuntimeError(f"render rc={returncode}; log tail:\n{tail}")

    # Point 4: download ra ĐÚNG TÊN nhập (tên này = khóa match khi import sang hệ live).
    dst = DOWNLOADS_DIR / f"{_safe_name(job['name'])}.mp4"
    if dst.exists():
        dst = DOWNLOADS_DIR / f"{_safe_name(job['name'])}_{job_id}.mp4"
    shutil.copy(out_path, dst)
    db.mark_done(job_id, str(dst))
    _log(f"job #{job_id} DONE -> {dst}")
    _start_drive_upload(job_id, dst, job.get("drive_folder"))


# ---------------------------------------------------------------- Google Drive

def _start_drive_upload(job_id, path, subfolder=None):
    """Đẩy video lên Drive ở thread nền — mạng chậm không được giữ GPU chờ job kế tiếp.

    Tên file trên Drive được chuẩn hóa ASCII; mọi khoảng trắng/dấu câu/ký tự đặc
    biệt được gộp thành một dấu gạch dưới. Tên file local không bị thay đổi.
    Thư mục con trong folder Drive chính (tự tạo nếu chưa có):
      - job import Excel: tên file Excel (cột drive_folder);
      - job render tay (không có drive_folder): gom theo ngày render 'dd-mm-yyyy'
        — không thả thẳng vào folder gốc cho đỡ loạn.
    Upload hỏng chỉ ghi drive_error vào DB (job vẫn done, file vẫn nằm ở downloads/);
    upload lại tay: python google_drive_upload.py <file> [--subfolder "<tên>"]. Thread là
    daemon nên tắt worker giữa chừng thì upload dở bị bỏ — chấp nhận, vì file gốc không mất.
    """
    if gdrive is None:
        _log(f"job #{job_id} skip Drive upload (google libs missing: {_GDRIVE_IMPORT_ERROR})")
        return
    if not gdrive.drive_enabled():
        return
    if not subfolder:
        subfolder = datetime.now().strftime("%d-%m-%Y")
    threading.Thread(
        target=_drive_upload,
        args=(job_id, path, subfolder),
        daemon=True,
    ).start()


def _drive_upload(job_id, path, subfolder=None):
    try:
        info = gdrive.upload_file(
            path, name=_drive_safe_name(path), subfolder=subfolder
        )
        db.set_drive_result(job_id, link=info.get("webViewLink"))
        where = f" [{subfolder}]" if subfolder else ""
        _log(f"job #{job_id} Drive upload OK{where} -> {info.get('webViewLink')}")
    except Exception as e:
        db.set_drive_result(job_id, error=e)
        _log(f"job #{job_id} Drive upload FAILED: {e}")


def _tail(path, n=15):
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-n:])
    except Exception:
        return "(no log)"


def _process_claimed_job(job):
    try:
        process_job(job)
    except Exception as exc:
        job_id = job["id"]
        retries = job["retries"]
        if retries < db.MAX_RETRIES:
            db.requeue_for_retry(job_id, exc)
            _log("job #{} failed (retry {}/{}): {}".format(
                job_id, retries + 1, db.MAX_RETRIES, exc))
        else:
            db.mark_failed(job_id, exc)
            _log("job #{} FAILED permanently: {}".format(job_id, exc))

def _collect_musetalk_pair(anchor):
    """Wait briefly for a second matching job created asynchronously by Excel/TTS."""
    if anchor.get("engine") != "musetalk" or not db.has_completed_matching_avatar(anchor):
        return []
    deadline = time.monotonic() + MUSETALK_BATCH_COLLECT_SECONDS
    while _RUNNING:
        matches = db.claim_matching_jobs(anchor, limit=1)
        if matches:
            waited = MUSETALK_BATCH_COLLECT_SECONDS - max(0.0, deadline - time.monotonic())
            _log(f"batch avatar: collected #{matches[0]['id']} after {waited:.1f}s")
            return matches
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return []
        time.sleep(min(MUSETALK_BATCH_POLL_SECONDS, remaining))


# ---------------------------------------------------------------- main loop

def _stop(*_):
    global _RUNNING
    _RUNNING = False
    _log("shutdown signal received; finishing current cycle...")


def main():
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    _ensure_dirs()
    if not _acquire_worker_lock():
        return

    db.init_db()
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

            # Warm a new avatar with one job first. Once warm, briefly collect a matching
            # partner that Excel/TTS may still be creating asynchronously.
            jobs = [job]
            jobs += _collect_musetalk_pair(job)
            if len(jobs) > 1:
                _log(f"batch avatar: running {len(jobs)} jobs concurrently: " +
                     ", ".join(f"#{item['id']}" for item in jobs))
            with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
                futures = [pool.submit(_process_claimed_job, item) for item in jobs]
                for future in as_completed(futures):
                    future.result()
        except Exception as loop_err:
            # Never let the loop die — log and keep going.
            _log(f"loop error (continuing): {loop_err}")
            time.sleep(POLL_SECONDS)

    _log("worker stopped.")


if __name__ == "__main__":
    main()
