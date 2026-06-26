"""Daemon TTS: lấy dòng từ tts_jobs, gọi TTS có RATE-LIMIT + RETRY + ADAPTIVE THROTTLE,
rồi tạo render job (jobs.db). Chạy song song với queue_worker.py (render GPU).

Khởi động:  ./venv/bin/python -u tts_worker.py
Cấu hình (env):
  AUSYNC_MAX_CONCURRENT   = 1      (số luồng — để 1 cho an toàn rate-limit)
  AUSYNC_MIN_INTERVAL_MS  = 1300   (khoảng cách tối thiểu giữa 2 request, ms)
  AUSYNC_THROTTLED_MS     = 3000   (nhịp khi đang bị giãn do 429)
  AUSYNC_MAX_RETRY        = 5      (số lần retry mỗi dòng)
"""
import os
import random
import signal
import time
import uuid

import tts_db
import tts_errors
import database as db
import excel_import as xi
from latentsync.tts.factory import synthesize, DEFAULT_PROVIDER

POLL_SECONDS = 2.0
MAX_CONCURRENT = int(os.getenv("AUSYNC_MAX_CONCURRENT", "1"))          # giữ 1: rate-limit an toàn
MIN_INTERVAL = float(os.getenv("AUSYNC_MIN_INTERVAL_MS", "1300")) / 1000.0
THROTTLED_INTERVAL = float(os.getenv("AUSYNC_THROTTLED_MS", "3000")) / 1000.0
MAX_RETRY = int(os.getenv("AUSYNC_MAX_RETRY", "5"))
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


def _process(job):
    provider = job["provider"] or DEFAULT_PROVIDER
    attempt = job["attempt_count"]

    if not _gate(provider):
        # bị dừng trước khi gọi -> trả về pending để lần chạy sau lấy lại
        tts_db.schedule_retry(job["id"], "worker dừng trước khi gọi TTS", 0)
        return

    audio_path = str(xi.TTS_AUDIO_DIR / f"tts_{job['id']}_{uuid.uuid4().hex[:8]}.wav")
    try:
        synthesize(text=job["text"], output_path=audio_path,
                   provider=provider, voice=job["voice_id"])
        if not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
            raise RuntimeError("TTS không tạo được file audio.")
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

    # TTS xong -> tạo render job trong hàng đợi GPU
    tts_db.note_success(provider)
    name = xi.build_name_excel(job["product"], job["video_type"], job["question_type"])
    render_id = db.add_job(name, job["video_path"], audio_path, **xi.RENDER_DEFAULTS)
    tts_db.mark_done(job["id"], audio_path, render_id)
    _log(f"#{job['id']} DONE -> render job #{render_id} ('{name}')")


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
