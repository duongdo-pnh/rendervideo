"""SQLite job store shared by web_ui.py (producer) and queue_worker.py (consumer).

WAL mode + busy_timeout lets the web UI read while the worker writes. The only
contended operation is claim_next_job(), which MUST be atomic so two workers
never grab the same job — see that function for the BEGIN IMMEDIATE pattern.
"""
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path(__file__).parent / "jobs.db"

# Nơi xuất video đã render — đặt TRÊN DESKTOP cho dễ tìm/nhìn. Cả "Render ngay" (web_ui)
# lẫn worker nền (queue_worker) đều ghi kết quả ra đây.
RENDERS_DIR = Path.home() / "Desktop" / "Renders"

# Maps the user's model choice (256/512) -> (config, checkpoint). Kept in sync with
# the MODELS dict in gradio_app.py. We persist the resolved paths per job so a later
# config change never retroactively rewrites what an already-queued job renders with.
MODELS = {
    "512": ("configs/unet/stage2_512.yaml", "checkpoints/latentsync_unet.pt"),
    "256": ("configs/unet/stage2.yaml", "checkpoints/v1.5/latentsync_unet.pt"),
}

STATUS_QUEUED = "queued"
STATUS_RENDERING = "rendering"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

MAX_RETRIES = 2  # render attempts beyond the first before a job is marked failed


def _connect():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")     # concurrent reader (web) + writer (worker)
    con.execute("PRAGMA busy_timeout=5000")    # wait, don't error, on a momentary write lock
    con.execute("PRAGMA foreign_keys=ON")
    return con


# Render config columns added after the first schema version, with their SQL defaults.
# init_db() ALTERs them in for older DBs so upgrades never lose existing jobs.
_CONFIG_COLUMNS = {
    "engine":         "TEXT    NOT NULL DEFAULT 'musetalk'",
    "guidance":       "REAL    NOT NULL DEFAULT 1.5",
    "steps":          "INTEGER NOT NULL DEFAULT 24",
    "seed":           "INTEGER NOT NULL DEFAULT 1247",
    "enhance_mouth":  "INTEGER NOT NULL DEFAULT 1",
    "enhance_region": "TEXT    NOT NULL DEFAULT 'mouth'",
    "out_res":        "TEXT    NOT NULL DEFAULT '720'",
    "input_type":     "TEXT    NOT NULL DEFAULT 'real'",
    # Google Drive auto-upload (queue_worker đẩy lên sau khi job done)
    "drive_link":     "TEXT",
    "drive_error":    "TEXT",
    "drive_folder":   "TEXT",   # tên thư mục con trong folder Drive chính (job từ Excel = tên file Excel); NULL = lên thẳng folder chính
    # (không có drive_name: file trên Drive luôn giữ đúng tên file đã lưu ở máy)
    "priority":       "INTEGER NOT NULL DEFAULT 0",  # lớn hơn = chạy trước
    # Backend (FastAPI) và worker (queue_worker.py) là 2 tiến trình khác nhau — backend
    # không thể kill trực tiếp subprocess render_job.py của worker. Đặt cờ này, worker tự
    # kiểm tra ở mỗi vòng poll (_run_render) rồi tự dừng subprocess của chính nó.
    "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
}


def _ensure_app_config_table(con):
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS app_config (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )


def get_config(key, default=None):
    """Per-install key/value state (e.g. the Drive OAuth refresh token).

    Lives in jobs.db, not .env: jobs.db is created fresh on every install and
    never bundled into the .deb, while .env can end up copied wholesale
    between machines (dev checkout copies, config backups) — which would
    silently carry the machine that built the app's Google account over to
    every install instead of each one authorizing its own.
    """
    with _connect() as con:
        _ensure_app_config_table(con)
        row = con.execute("SELECT value FROM app_config WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_config(key, value):
    with _connect() as con:
        _ensure_app_config_table(con)
        con.execute(
            "INSERT INTO app_config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def init_db():
    """Create the jobs table if missing + migrate config columns. Idempotent."""
    with _connect() as con:
        _ensure_app_config_table(con)
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT    NOT NULL,
                video_path      TEXT    NOT NULL,
                audio_path      TEXT    NOT NULL,
                model_res       TEXT    NOT NULL,
                config_path     TEXT    NOT NULL,
                checkpoint_path TEXT    NOT NULL,
                status          TEXT    NOT NULL DEFAULT 'queued',
                retries         INTEGER NOT NULL DEFAULT 0,
                error           TEXT,
                output_path     TEXT,
                created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
                started_at      TEXT,
                finished_at     TEXT
            )
            """
        )
        # Migrate render-config columns onto pre-existing tables (no-op on fresh DBs).
        existing = {r["name"] for r in con.execute("PRAGMA table_info(jobs)")}
        for col, decl in _CONFIG_COLUMNS.items():
            if col not in existing:
                con.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")
        # Speeds up claim_next_job: trạng thái + ưu tiên giảm dần + thứ tự tạo.
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_status_priority_created ON jobs(status, priority DESC, created_at, id)"
        )


def add_job(name, video_path, audio_path, model_res, guidance=1.5, steps=24, seed=1247,
            enhance_mouth=1, enhance_region="mouth", out_res="720", input_type="real",
            drive_folder=None, engine="musetalk"):
    """Insert a new render job with its full render config."""
    engine = str(engine)
    if engine not in ("musetalk", "latentsync"):
        raise ValueError(f"unknown engine {engine!r} (expected 'musetalk' or 'latentsync')")
    model_res = str(model_res)
    if model_res not in MODELS:
        raise ValueError(f"unknown model_res {model_res!r} (expected one of {list(MODELS)})")
    config_path, checkpoint_path = MODELS[model_res]
    with _connect() as con:
        cur = con.execute(
            """
            INSERT INTO jobs (name, video_path, audio_path, model_res, config_path, checkpoint_path, engine,
                              guidance, steps, seed, enhance_mouth, enhance_region, out_res, input_type,
                              drive_folder)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (name, str(video_path), str(audio_path), model_res, config_path, checkpoint_path, str(engine),
             float(guidance), int(steps), int(seed), int(bool(enhance_mouth)),
             str(enhance_region), str(out_res), "ai" if input_type == "ai" else "real",
             str(drive_folder) if drive_folder else None),
        )
        return cur.lastrowid


def claim_next_job():
    """Atomically grab the oldest queued job and flip it to 'rendering'. Returns a dict or None.

    BEGIN IMMEDIATE takes the write lock up front, so the SELECT-then-UPDATE pair can't
    interleave with another worker's claim. The single UPDATE...RETURNING does the flip and
    hands back the full row in one statement — no separate read that could see a stale status.
    """
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            """
            UPDATE jobs
               SET status = 'rendering',
                   started_at = datetime('now','localtime'),
                   finished_at = NULL,
                   error = NULL
             WHERE id = (
                   SELECT id FROM jobs
                    WHERE status = 'queued'
                    ORDER BY priority DESC, created_at, id
                    LIMIT 1
             )
            RETURNING *
            """
        ).fetchone()
        con.commit()
        return dict(row) if row else None
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def has_completed_matching_avatar(anchor):
    """True after one successful MuseTalk render has warmed this avatar/resolution."""
    if anchor.get("engine") != "musetalk":
        return False
    with _connect() as con:
        return con.execute(
            "SELECT 1 FROM jobs WHERE status='done' AND engine='musetalk' "
            "AND video_path=? AND out_res=? LIMIT 1",
            (anchor["video_path"], anchor["out_res"]),
        ).fetchone() is not None


def claim_matching_jobs(anchor, limit=2):
    """Atomically claim queued MuseTalk jobs that can share the anchor avatar."""
    if limit <= 0 or anchor.get("engine") != "musetalk":
        return []
    con = _connect()
    try:
        con.execute("BEGIN IMMEDIATE")
        rows = con.execute(
            "SELECT id FROM jobs WHERE status=\x27queued\x27 AND engine=\x27musetalk\x27 "
            "AND video_path=? AND out_res=? ORDER BY created_at,id LIMIT ?",
            (anchor["video_path"], anchor["out_res"], int(limit)),
        ).fetchall()
        claimed = []
        for row in rows:
            item = con.execute(
                "UPDATE jobs SET status=\x27rendering\x27, started_at=datetime(\x27now\x27,\x27localtime\x27), "
                "finished_at=NULL,error=NULL WHERE id=? AND status=\x27queued\x27 RETURNING *",
                (row["id"],),
            ).fetchone()
            if item:
                claimed.append(dict(item))
        con.commit()
        return claimed
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def mark_done(job_id, output_path):
    with _connect() as con:
        con.execute(
            "UPDATE jobs SET status='done', output_path=?, error=NULL, "
            "finished_at=datetime('now','localtime') WHERE id=?",
            (str(output_path), job_id),
        )


def mark_failed(job_id, error):
    with _connect() as con:
        con.execute(
            "UPDATE jobs SET status='failed', error=?, "
            "finished_at=datetime('now','localtime') WHERE id=?",
            (str(error)[:2000], job_id),
        )


def mark_cancelled(job_id, error=None):
    """Worker gọi sau khi thật sự dừng xong subprocess của 1 job 'rendering' bị hủy."""
    with _connect() as con:
        con.execute(
            "UPDATE jobs SET status='cancelled', error=?, cancel_requested=0, "
            "finished_at=datetime('now','localtime') WHERE id=?",
            (str(error) if error else "Đã hủy theo yêu cầu người dùng.", job_id),
        )


def request_cancel(job_id):
    """Hủy 1 job theo yêu cầu người dùng.

    'queued' -> hủy NGAY (chưa render gì, không cần worker). 'rendering' -> chỉ đặt cờ
    cancel_requested; worker tự phát hiện ở vòng poll kế tiếp (tối đa vài giây) rồi mới
    thật sự dừng subprocess và gọi mark_cancelled(). Job đã done/failed/cancelled thì
    không còn gì để hủy — raise ValueError.

    Trả về status TRƯỚC khi hủy ('queued' hoặc 'rendering'), để caller biết hủy ngay hay
    phải chờ worker.
    """
    with _connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError(f"Không thấy job #{job_id}.")
        status = row["status"]
        if status == STATUS_QUEUED:
            con.execute(
                "UPDATE jobs SET status='cancelled', error='Đã hủy theo yêu cầu người dùng.', "
                "finished_at=datetime('now','localtime') WHERE id=?",
                (job_id,),
            )
        elif status == STATUS_RENDERING:
            con.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (job_id,))
        else:
            raise ValueError(f"Job #{job_id} đã ở trạng thái '{status}' — không có gì để hủy.")
        return status


def request_cancel_by_drive_folder(drive_folder):
    """Huỷ MỌI render job đang queued/rendering của một batch, tìm theo drive_folder.
    Trả list id đã tác động. Không raise nếu không có gì để huỷ.

    drive_folder là tên file Excel bỏ đuôi (tts_worker.py đặt lúc tạo render job),
    và akali_bridge chèn '__job<akali_job_id>' vào tên Excel nên nó là khoá DUY NHẤT
    cho một batch akali. Dùng khoá này thay vì đi từ tts_jobs vì nó bắt được cả
    render job "mồ côi": tts_worker gọi add_job() TRƯỚC mark_done(), nên một dòng
    tts_jobs bị xoá đúng lúc worker kẹt giữa hai lệnh đó vẫn để lại render job mà
    không còn dòng nào trỏ tới — đi từ tts_jobs sẽ bỏ sót đúng job đang đốt GPU.

    Cùng quy ước với request_cancel(): 'queued' huỷ ngay tại chỗ, 'rendering' chỉ
    đặt cờ để queue_worker tự dừng subprocess của chính nó ở vòng poll kế tiếp.
    """
    with _connect() as con:
        con.execute("BEGIN IMMEDIATE")
        rows = con.execute(
            "SELECT id, status FROM jobs WHERE drive_folder=? AND status IN (?, ?)",
            (drive_folder, STATUS_QUEUED, STATUS_RENDERING),
        ).fetchall()
        for row in rows:
            if row["status"] == STATUS_QUEUED:
                con.execute(
                    "UPDATE jobs SET status='cancelled', error='Batch bị huỷ từ akali.', "
                    "finished_at=datetime('now','localtime') WHERE id=?",
                    (row["id"],),
                )
            else:
                con.execute("UPDATE jobs SET cancel_requested=1 WHERE id=?", (row["id"],))
        return [row["id"] for row in rows]


def is_cancel_requested(job_id):
    with _connect() as con:
        row = con.execute("SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)).fetchone()
        return bool(row and row["cancel_requested"])


def cancel_all_active():
    """Hủy TẤT CẢ job đang chờ (ngay lập tức) + đặt cờ cho job đang render (worker tự dừng).

    Trả về (n_queued, n_rendering) — số job hủy ngay và số job vừa được đặt cờ.
    """
    with _connect() as con:
        con.execute("BEGIN IMMEDIATE")
        n_queued = con.execute(
            "UPDATE jobs SET status='cancelled', error='Đã hủy theo yêu cầu người dùng.', "
            "finished_at=datetime('now','localtime') WHERE status='queued'"
        ).rowcount
        n_rendering = con.execute(
            "UPDATE jobs SET cancel_requested=1 WHERE status='rendering' AND cancel_requested=0"
        ).rowcount
        return n_queued, n_rendering


def set_drive_result(job_id, link=None, error=None):
    """Ghi kết quả upload Google Drive của 1 job done: link khi thành công, error khi hỏng."""
    with _connect() as con:
        con.execute(
            "UPDATE jobs SET drive_link=?, drive_error=? WHERE id=?",
            (link, str(error)[:2000] if error else None, job_id),
        )


def requeue_for_retry(job_id, error):
    """Bump retry count and put the job back to 'queued' so the worker picks it up again."""
    with _connect() as con:
        con.execute(
            "UPDATE jobs SET status='queued', retries=retries+1, error=?, "
            "started_at=NULL WHERE id=?",
            (str(error)[:2000], job_id),
        )


def prioritize_job(job_id):
    """Đưa một job đang chờ lên đầu queue. Job đang render hoặc đã xong bị từ chối."""
    with _connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError(f"Không thấy job #{job_id}.")
        if row["status"] != STATUS_QUEUED:
            raise ValueError(
                f"Job #{job_id} không ở trạng thái chờ (hiện tại: {row['status']})."
            )
        next_priority = con.execute(
            "SELECT COALESCE(MAX(priority), 0) + 1 FROM jobs WHERE status=?",
            (STATUS_QUEUED,),
        ).fetchone()[0]
        con.execute("UPDATE jobs SET priority=? WHERE id=?", (next_priority, job_id))
        return {**dict(row), "priority": next_priority}


def prioritize_jobs(job_ids):
    """Xếp nhiều job queued theo thứ tự truyền vào; phần tử đầu chạy trước."""
    ordered_ids = list(dict.fromkeys(int(job_id) for job_id in (job_ids or [])))
    if not ordered_ids:
        raise ValueError("Hãy chọn ít nhất một job đang chờ.")
    with _connect() as con:
        con.execute("BEGIN IMMEDIATE")
        placeholders = ",".join("?" for _ in ordered_ids)
        rows = con.execute(
            f"SELECT id, status FROM jobs WHERE id IN ({placeholders})", ordered_ids
        ).fetchall()
        found = {row["id"]: row["status"] for row in rows}
        missing = [job_id for job_id in ordered_ids if job_id not in found]
        if missing:
            raise ValueError(f"Không thấy job: {missing}.")
        invalid = [job_id for job_id in ordered_ids if found[job_id] != STATUS_QUEUED]
        if invalid:
            raise ValueError(f"Job không còn ở trạng thái chờ: {invalid}.")
        base = con.execute(
            "SELECT COALESCE(MAX(priority), 0) FROM jobs WHERE status=?",
            (STATUS_QUEUED,),
        ).fetchone()[0]
        total = len(ordered_ids)
        for position, job_id in enumerate(ordered_ids):
            con.execute(
                "UPDATE jobs SET priority=? WHERE id=?",
                (base + total - position, job_id),
            )
    return ordered_ids


def retry_job(job_id):
    """Đưa 1 job 'failed' về 'queued' để worker thử lại — reset retries về 0 vì đây là
    lần thử mới do người dùng chủ động yêu cầu (thường sau khi đã sửa nguyên nhân gốc,
    vd. thiếu checkpoint), không phải tiếp tục đếm retry tự động cũ."""
    with _connect() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError(f"Không thấy job #{job_id}.")
        if row["status"] != STATUS_FAILED:
            raise ValueError(f"Job #{job_id} không ở trạng thái lỗi (hiện tại: {row['status']}).")
        updated = con.execute(
            "UPDATE jobs SET status='queued', retries=0, error=NULL, "
            "started_at=NULL, finished_at=NULL WHERE id=? RETURNING *",
            (job_id,),
        ).fetchone()
        return dict(updated)


def get_job(job_id):
    with _connect() as con:
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None


def list_jobs(limit=200):
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def list_rendering_jobs():
    """Job(s) actually in progress right now — queried by status directly, not by
    recency, because list_jobs(limit=N)'s `ORDER BY id DESC` can push a long-running
    render out of the window once N+ newer 'queued' rows land from a big Excel batch
    (single GPU means the in-progress job is usually the OLDEST 'rendering'/'queued'
    row, not a recent one)."""
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY id", (STATUS_RENDERING,)
        ).fetchall()
        return [dict(r) for r in rows]


def list_done_jobs(limit=200):
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM jobs WHERE status='done' AND output_path IS NOT NULL "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def stats_summary():
    """done/failed hôm nay/7 ngày/tháng này/năm nay + trend 14 ngày gần nhất — dùng để
    push lên dashboard fleet (xem doc_api/API_INTEGRATION.md). Dashboard tự cộng dồn
    qua mọi node nên đây chỉ cần đúng số liệu của MÁY NÀY."""
    def _count(con, where_extra, args=()):
        rows = con.execute(
            f"SELECT status, COUNT(*) c FROM jobs WHERE status IN ('done','failed') "
            f"AND finished_at IS NOT NULL {where_extra} GROUP BY status",
            args,
        ).fetchall()
        counts = {"done": 0, "failed": 0}
        for r in rows:
            counts[r["status"]] = r["c"]
        return counts

    with _connect() as con:
        today = _count(con, "AND date(finished_at) = date('now','localtime')")
        this_week = _count(con, "AND date(finished_at) >= date('now','localtime','-6 days')")
        this_month = _count(con, "AND strftime('%Y-%m', finished_at) = strftime('%Y-%m','now','localtime')")
        this_year = _count(con, "AND strftime('%Y', finished_at) = strftime('%Y','now','localtime')")
        trend_rows = con.execute(
            "SELECT date(finished_at) d, status, COUNT(*) c FROM jobs "
            "WHERE status IN ('done','failed') AND finished_at IS NOT NULL "
            "AND date(finished_at) >= date('now','localtime','-13 days') "
            "GROUP BY d, status"
        ).fetchall()

    by_date = {}
    for r in trend_rows:
        by_date.setdefault(r["d"], {"done": 0, "failed": 0})[r["status"]] = r["c"]
    today_date = datetime.now().date()
    daily_trend = []
    for i in range(13, -1, -1):
        d = today_date - timedelta(days=i)
        entry = by_date.get(d.isoformat(), {"done": 0, "failed": 0})
        daily_trend.append({"date": d.strftime("%d/%m"), "done": entry["done"], "failed": entry["failed"]})

    return {
        "today": today, "this_week": this_week, "this_month": this_month, "this_year": this_year,
        "daily_trend": daily_trend,
    }


def list_stale_done_jobs(older_than_days):
    """Job 'done' đã render xong quá `older_than_days` ngày (theo finished_at) và còn
    output_path — dùng để dọn video cũ khỏi ổ đĩa định kỳ (queue_worker.cleanup_old_renders)."""
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM jobs WHERE status='done' AND output_path IS NOT NULL "
            "AND finished_at IS NOT NULL "
            "AND finished_at < datetime('now', 'localtime', ?)",
            (f"-{int(older_than_days)} days",),
        ).fetchall()
        return [dict(r) for r in rows]


def list_stale_upload_jobs(older_than_days):
    """Job đã kết thúc (done/failed/cancelled) quá `older_than_days` ngày (theo
    finished_at) — dùng để dọn file input đã upload khỏi ổ đĩa định kỳ
    (queue_worker.cleanup_old_uploads). failed/cancelled cũng tính vào đây: retry
    chỉ còn tự động khi job vẫn 'queued' (MAX_RETRIES đã dùng hết mới thành
    'failed' thật sự), nút Retry thủ công trên 1 job failed lâu ngày vẫn hoạt
    động nhưng sẽ báo lỗi thiếu file input nếu bấm sau khi hạn này đã qua."""
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM jobs WHERE status IN ('done','failed','cancelled') "
            "AND finished_at IS NOT NULL "
            "AND finished_at < datetime('now', 'localtime', ?)",
            (f"-{int(older_than_days)} days",),
        ).fetchall()
        return [dict(r) for r in rows]


def clear_output_path(job_id):
    """Gỡ tham chiếu output_path sau khi file đã bị dọn khỏi đĩa — job vẫn giữ 'done',
    chỉ không còn trỏ tới file không còn tồn tại."""
    with _connect() as con:
        con.execute("UPDATE jobs SET output_path=NULL WHERE id=?", (job_id,))


def reset_stuck_jobs():
    """On worker startup, requeue jobs left in 'rendering' by a previous crash/restart."""
    with _connect() as con:
        cur = con.execute(
            "UPDATE jobs SET status='queued', started_at=NULL WHERE status='rendering'"
        )
        return cur.rowcount


def worker_busy():
    """True nếu worker đang render 1 job trong queue (status='rendering').
    web_ui dùng để KHÔNG 'Render ngay' song song với worker (tránh tranh GPU)."""
    with _connect() as con:
        return con.execute(
            "SELECT 1 FROM jobs WHERE status='rendering' LIMIT 1"
        ).fetchone() is not None


def delete_job(job_id):
    """Xóa 1 job. TỪ CHỐI job đang 'rendering' (worker đang giữ nó).
    Trả về dict của hàng đã xóa (kèm đường dẫn file) để caller dọn file; None nếu không tìm thấy."""
    with _connect() as con:
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        if row["status"] == STATUS_RENDERING:
            raise ValueError(f"Job #{job_id} đang render — không thể xóa (chờ xong hoặc dừng worker).")
        con.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        return dict(row)


def clear_jobs(status):
    """Xóa hàng loạt job theo trạng thái (không cho xóa 'rendering'). Trả về list các hàng đã xóa."""
    if status == STATUS_RENDERING:
        raise ValueError("Không thể xóa hàng loạt job đang render.")
    with _connect() as con:
        rows = con.execute("SELECT * FROM jobs WHERE status=?", (status,)).fetchall()
        con.execute("DELETE FROM jobs WHERE status=?", (status,))
        return [dict(r) for r in rows]


if __name__ == "__main__":
    init_db()
    print(f"Initialized {DB_PATH}")
