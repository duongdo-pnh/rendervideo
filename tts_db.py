"""Durable TTS queue + global rate limiter (SQLite, dùng chung jobs.db với render queue).

Tách bước TTS ra khỏi submit_jobs: Excel chỉ ENQUEUE vào tts_jobs; một daemon
(tts_worker.py) gọi TTS có rate-limit + retry rồi tạo render job. Mỗi dòng có state
riêng nên không bao giờ "mất dòng" vì lỗi tạm thời, và resume được sau khi crash.

State của tts_jobs:
  pending           - chờ xử lý
  submitting        - worker đang gọi TTS (claim)
  retry_wait        - lỗi tạm thời, chờ tới next_attempt_at để thử lại
  done              - đã tạo audio + đẩy render job (render_job_id)
  failed_retryable  - dead-letter: hết lượt retry mà vẫn lỗi tạm thời
  failed_permanent  - lỗi cứng (payload/voice/quyền), không thử lại

tts_rate_limit: 1 dòng/provider, điều phối chung mọi process qua BEGIN IMMEDIATE.
"""
import time

import sqlite3

from database import DB_PATH


def _connect():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    return con


def init_db():
    """Tạo bảng tts_jobs + tts_rate_limit nếu chưa có. Idempotent."""
    with _connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS tts_jobs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id        TEXT,
                excel_row       INTEGER,
                text            TEXT    NOT NULL,
                provider        TEXT,
                voice_id        TEXT,
                product         TEXT,
                video_path      TEXT    NOT NULL,
                video_type      TEXT,
                question_type   TEXT,
                status          TEXT    NOT NULL DEFAULT 'pending',
                attempt_count   INTEGER NOT NULL DEFAULT 0,
                last_error      TEXT,
                audio_path      TEXT,
                render_job_id   INTEGER,
                next_attempt_at REAL    NOT NULL DEFAULT 0,
                created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
                updated_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_tts_status_next ON tts_jobs(status, next_attempt_at, id)"
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS tts_rate_limit (
                provider        TEXT PRIMARY KEY,
                last_request_at REAL NOT NULL DEFAULT 0,
                throttle_until  REAL NOT NULL DEFAULT 0,
                interval_until  REAL NOT NULL DEFAULT 0,
                recent_429      INTEGER NOT NULL DEFAULT 0
            )
            """
        )


# ------------------------------------------------------------------ enqueue / claim

def enqueue(batch_id, excel_row, text, provider, voice_id,
            product, video_path, video_type, question_type):
    with _connect() as con:
        cur = con.execute(
            """
            INSERT INTO tts_jobs (batch_id, excel_row, text, provider, voice_id,
                                  product, video_path, video_type, question_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (batch_id, excel_row, text, provider, voice_id,
             product, str(video_path), video_type, question_type),
        )
        return cur.lastrowid


def claim_next():
    """Atomically lấy job pending/retry_wait đã tới hạn (next_attempt_at<=now) -> 'submitting'."""
    now = time.time()
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            """
            UPDATE tts_jobs
               SET status='submitting', updated_at=datetime('now','localtime')
             WHERE id = (
                   SELECT id FROM tts_jobs
                    WHERE status IN ('pending','retry_wait') AND next_attempt_at <= ?
                    ORDER BY next_attempt_at, id
                    LIMIT 1
             )
            RETURNING *
            """,
            (now,),
        ).fetchone()
        con.commit()
        return dict(row) if row else None
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def mark_done(job_id, audio_path, render_job_id):
    with _connect() as con:
        con.execute(
            "UPDATE tts_jobs SET status='done', audio_path=?, render_job_id=?, last_error=NULL, "
            "updated_at=datetime('now','localtime') WHERE id=?",
            (str(audio_path), int(render_job_id), job_id),
        )


def schedule_retry(job_id, error, delay_seconds):
    """Lỗi tạm thời -> retry_wait, tăng attempt_count, hẹn next_attempt_at = now + delay."""
    with _connect() as con:
        con.execute(
            "UPDATE tts_jobs SET status='retry_wait', attempt_count=attempt_count+1, "
            "last_error=?, next_attempt_at=?, updated_at=datetime('now','localtime') WHERE id=?",
            (str(error)[:2000], time.time() + max(0.0, delay_seconds), job_id),
        )


def mark_failed(job_id, error, permanent):
    """permanent=True -> failed_permanent (lỗi cứng); False -> failed_retryable (hết lượt retry)."""
    status = "failed_permanent" if permanent else "failed_retryable"
    with _connect() as con:
        con.execute(
            "UPDATE tts_jobs SET status=?, attempt_count=attempt_count+1, last_error=?, "
            "updated_at=datetime('now','localtime') WHERE id=?",
            (status, str(error)[:2000], job_id),
        )


def reset_stuck():
    """Đưa job 'submitting' (worker chết giữa chừng) về 'pending' để chạy lại. Trả số dòng."""
    with _connect() as con:
        cur = con.execute(
            "UPDATE tts_jobs SET status='pending', next_attempt_at=0, "
            "updated_at=datetime('now','localtime') WHERE status='submitting'"
        )
        return cur.rowcount


# ------------------------------------------------------------------ views (UI/CLI)

def status_counts(batch_id=None):
    q = "SELECT status, COUNT(*) c FROM tts_jobs"
    args = ()
    if batch_id:
        q += " WHERE batch_id=?"
        args = (batch_id,)
    q += " GROUP BY status"
    with _connect() as con:
        return {r["status"]: r["c"] for r in con.execute(q, args)}


def list_jobs(limit=200, status=None):
    q = "SELECT * FROM tts_jobs"
    args = []
    if status:
        q += " WHERE status=?"
        args.append(status)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    with _connect() as con:
        return [dict(r) for r in con.execute(q, args)]


def requeue_dead_letter(batch_id=None):
    """Đưa failed_retryable (dead-letter) về pending để chạy lại có kiểm soát. Trả số dòng."""
    q = ("UPDATE tts_jobs SET status='pending', next_attempt_at=0, attempt_count=0, "
         "updated_at=datetime('now','localtime') WHERE status='failed_retryable'")
    args = ()
    if batch_id:
        q += " AND batch_id=?"
        args = (batch_id,)
    with _connect() as con:
        return con.execute(q, args).rowcount


# ------------------------------------------------------------------ global rate limiter

def _ensure_provider(con, provider):
    con.execute("INSERT OR IGNORE INTO tts_rate_limit(provider) VALUES (?)", (provider,))


def reserve_slot(provider, base_interval, throttled_interval):
    """Thử giữ chỗ gửi request kế tiếp cho provider (điều phối qua toàn bộ process).

    Trả 0.0 nếu được phép gọi NGAY (đã ghi nhận thời điểm); ngược lại trả số GIÂY caller
    cần ngủ rồi gọi lại. Trong cửa sổ throttle/widen thì giãn nhịp tự động (adaptive).
    """
    now = time.time()
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        _ensure_provider(con, provider)
        r = con.execute(
            "SELECT last_request_at, throttle_until, interval_until FROM tts_rate_limit WHERE provider=?",
            (provider,),
        ).fetchone()
        last, throttle_until, interval_until = r["last_request_at"], r["throttle_until"], r["interval_until"]
        interval = throttled_interval if now < interval_until else base_interval
        ready_at = max(throttle_until, last + interval)
        if now < ready_at:
            con.commit()
            return ready_at - now
        con.execute("UPDATE tts_rate_limit SET last_request_at=? WHERE provider=?", (now, provider))
        con.commit()
        return 0.0
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def note_rate_limited(provider, widen_window=60.0, pause_threshold=3, pause_seconds=180.0):
    """Gặp 429: giãn nhịp trong widen_window giây; nếu dồn >=pause_threshold lần thì pause lâu."""
    now = time.time()
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        _ensure_provider(con, provider)
        r = con.execute("SELECT recent_429 FROM tts_rate_limit WHERE provider=?", (provider,)).fetchone()
        recent = (r["recent_429"] if r else 0) + 1
        throttle_add = 0.0
        if recent >= pause_threshold:
            throttle_add = pause_seconds
            recent = 0
        con.execute(
            "UPDATE tts_rate_limit SET recent_429=?, interval_until=?, "
            "throttle_until=MAX(throttle_until, ?) WHERE provider=?",
            (recent, now + widen_window, now + throttle_add, provider),
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def note_success(provider):
    """Thành công: hạ dần bộ đếm 429 (thoát chế độ giãn nhịp khi API ổn lại)."""
    with _connect() as con:
        _ensure_provider(con, provider)
        con.execute("UPDATE tts_rate_limit SET recent_429=MAX(0, recent_429-1) WHERE provider=?", (provider,))
