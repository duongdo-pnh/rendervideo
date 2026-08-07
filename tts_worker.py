"""Daemon TTS: lấy dòng từ tts_jobs, gọi TTS có RATE-LIMIT + RETRY + ADAPTIVE THROTTLE,
rồi tạo render job (jobs.db). Chạy song song với queue_worker.py (render GPU).

Khởi động:  ./venv/bin/python -u tts_worker.py
Cấu hình (env):
  AUSYNC_MAX_CONCURRENT   = 1      (số luồng — để 1 cho an toàn rate-limit)
  AUSYNC_MIN_INTERVAL_MS  = 1300   (khoảng cách tối thiểu giữa 2 request, ms)
  AUSYNC_THROTTLED_MS     = 3000   (nhịp khi đang bị giãn do 429)
  AUSYNC_MAX_RETRY        = 5      (số lần retry mỗi dòng)
"""
import json
import os
import random
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager

# Ensure ffprobe/ffmpeg from the current conda env are visible even when the
# worker is launched directly by absolute python path.
_envbin = os.path.dirname(sys.executable)
if _envbin and _envbin not in os.environ.get("PATH", "").split(os.pathsep):
    os.environ["PATH"] = _envbin + os.pathsep + os.environ.get("PATH", "")

import tts_db
import tts_errors
import database as db
import excel_import as xi
from latentsync.tts import factory

POLL_SECONDS = 2.0


def _num_env(name, default, cast=float):
    value = os.getenv(name)
    value = default if value is None or str(value).strip() == "" else value
    return cast(value)


MAX_CONCURRENT = _num_env("AUSYNC_MAX_CONCURRENT", 1, int)          # giữ 1: rate-limit an toàn
MIN_INTERVAL = _num_env("AUSYNC_MIN_INTERVAL_MS", 1300) / 1000.0
THROTTLED_INTERVAL = _num_env("AUSYNC_THROTTLED_MS", 3000) / 1000.0
MAX_RETRY = _num_env("AUSYNC_MAX_RETRY", 5, int)
JOB_TIMEOUT = _num_env("TTS_JOB_TIMEOUT", 600)                         # hard cap mỗi dòng, tránh request treo mãi
BACKOFF = [2, 5, 10, 20, 30]      # giây theo lần retry (attempt_count); + jitter 0–1.5s

_RUNNING = True


def _log(msg):
    print(f"[tts {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _stop(*_):
    global _RUNNING
    _RUNNING = False
    _log("nhận tín hiệu dừng, sẽ thoát sau job hiện tại...")


def _gate(provider):
    """Chờ tới lượt theo rate limiter toàn cục. Trả False nếu bị yêu cầu dừng giữa chừng."""
    while _RUNNING:
        wait = tts_db.reserve_slot(provider, MIN_INTERVAL, THROTTLED_INTERVAL)
        if wait <= 0:
            return True
        time.sleep(min(wait, 5.0))      # ngủ ngắn để còn phản hồi tín hiệu dừng
    return False


def _backoff_delay(attempt):
    base = BACKOFF[min(attempt, len(BACKOFF) - 1)]
    return base + random.uniform(0, 1.5)     # jitter tránh nhiều dòng "tỉnh" cùng lúc


def _audio_duration(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float((out.stdout or "").strip() or 0)
    except Exception:
        return 0.0


def _validate_tts_audio(job, audio_path):
    duration = _audio_duration(audio_path)
    if duration <= 0:
        raise RuntimeError("TTS tạo audio nhưng không đọc được duration.")

    # Guard against provider glitches: a short live sentence should not become
    # a 2-minute audio with long gaps. Keep the threshold loose for normal long scripts.
    text_len = len(job["text"] or "")
    max_expected = max(45.0, text_len / 4.0)
    if duration > max_expected:
        raise RuntimeError(
            f"TTS audio bất thường: dài {duration:.1f}s cho {text_len} ký tự "
            f"(ngưỡng {max_expected:.1f}s). Không đẩy qua render."
        )
    return duration


@contextmanager
def _hard_timeout(seconds, label):
    """Cắt một job TTS nếu thư viện HTTP/provider bị treo quá lâu.

    requests đã có timeout từng call, nhưng thực tế kết nối/CDN đôi khi giữ socket lâu hơn
    kỳ vọng. SIGALRM giúp worker không bị kẹt ở trạng thái submitting vô hạn.
    """
    if not seconds or seconds <= 0:
        yield
        return

    old_handler = signal.getsignal(signal.SIGALRM)

    def _raise_timeout(_signum, _frame):
        raise TimeoutError(f"{label} quá thời gian chờ hard-timeout {int(seconds)}s")

    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)


def _process(job):
    factory.reload_config()
    provider = job["provider"] or factory.DEFAULT_PROVIDER
    attempt = job["attempt_count"]

    if not _gate(provider):
        # bị dừng trước khi gọi -> trả về pending để lần chạy sau lấy lại
        tts_db.schedule_retry(job["id"], "worker dừng trước khi gọi TTS", 0)
        return

    audio_path = str(xi.TTS_AUDIO_DIR / f"tts_{job['id']}_{uuid.uuid4().hex[:8]}.wav")
    try:
        with _hard_timeout(JOB_TIMEOUT, f"TTS job #{job['id']}"):
            factory.synthesize(text=job["text"], output_path=audio_path,
                               provider=provider, voice=job["voice_id"])
        if not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
            raise RuntimeError("TTS không tạo được file audio.")
        duration = _validate_tts_audio(job, audio_path)
    except Exception as e:
        retryable = tts_errors.is_retryable(e)
        if tts_errors.parse_status(e) == 429:
            tts_db.note_rate_limited(provider)        # adaptive: giãn nhịp / pause
        if retryable and attempt < MAX_RETRY:
            ra = tts_errors.parse_retry_after(e)
            delay = (ra + random.uniform(0, 1.5)) if ra is not None else _backoff_delay(attempt)
            tts_db.schedule_retry(job["id"], e, delay)
            _log(f"#{job['id']} lỗi tạm thời (retry {attempt + 1}/{MAX_RETRY} sau {delay:.1f}s): {e}")
        else:
            tts_db.mark_failed(job["id"], e, permanent=not retryable)
            kind = "lỗi cứng" if not retryable else f"hết {MAX_RETRY} lượt retry"
            _log(f"#{job['id']} FAILED ({kind}) -> dead-letter: {e}")
        return

    # TTS xong -> tạo render job trong hàng đợi GPU (dùng cấu hình render của batch nếu có)
    tts_db.note_success(provider)
    name = xi.build_name_excel(job["product"], job["video_type"], job["question_type"],
                               job.get("other_key"), job.get("excel_row"))
    cfg = dict(xi.RENDER_DEFAULTS)
    if job.get("render_config"):
        try:
            cfg.update(json.loads(job["render_config"]))
        except Exception:
            pass
    # Job từ Excel: video lên Drive vào thư mục con mang tên file Excel của batch.
    drive_folder = None
    if job.get("batch_id"):
        excel_path = tts_db.get_batch_excel(job["batch_id"])
        if excel_path:
            drive_folder = os.path.splitext(os.path.basename(excel_path))[0].strip() or None
    render_id = db.add_job(name, job["video_path"], audio_path, drive_folder=drive_folder, **cfg)
    tts_db.mark_done(job["id"], audio_path, render_id)
    _log(f"#{job['id']} DONE ({duration:.1f}s) -> render job #{render_id} ('{name}')")


def main():
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    db.init_db()
    tts_db.init_db()
    xi.TTS_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    n = tts_db.reset_stuck()
    if n:
        _log(f"đưa lại {n} job 'submitting' kẹt về pending (resume sau crash)")
    _log(f"tts worker started; concurrent={MAX_CONCURRENT}, interval={MIN_INTERVAL}s, max_retry={MAX_RETRY}")

    while _RUNNING:
        try:
            job = tts_db.claim_next()
            if not job:
                time.sleep(POLL_SECONDS)
                continue
            _process(job)
        except Exception as loop_err:
            _log(f"loop error (tiếp tục): {loop_err}")
            time.sleep(POLL_SECONDS)

    _log("tts worker stopped.")


if __name__ == "__main__":
    main()
