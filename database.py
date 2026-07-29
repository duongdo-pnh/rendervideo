"""SQLite job store shared by web_ui.py (producer) and queue_worker.py (consumer).

WAL mode + busy_timeout lets the web UI read while the worker writes. The only
contended operation is claim_next_job(), which MUST be atomic so two workers
never grab the same job — see that function for the BEGIN IMMEDIATE pattern.
"""
import sqlite3
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
    "drive_name":     "TEXT",   # tên file riêng khi upload Drive; NULL = giữ tên file local
    "priority":       "INTEGER NOT NULL DEFAULT 0",  # lớn hơn = chạy trước
}


def init_db():
    """Create the jobs table if missing + migrate config columns. Idempotent."""
    with _connect() as con:
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
            drive_folder=None, drive_name=None, engine="musetalk"):
    """Insert a new MuseTalk job with its full render config."""
    # Normal/Excel/TTS renders are MuseTalk-only. Keep the argument for backward
    # compatibility with existing callers, but never persist another engine.
    engine = "musetalk"
    model_res = str(model_res)
    if model_res not in MODELS:
        raise ValueError(f"unknown model_res {model_res!r} (expected one of {list(MODELS)})")
    config_path, checkpoint_path = MODELS[model_res]
    with _connect() as con:
        cur = con.execute(
            """
            INSERT INTO jobs (name, video_path, audio_path, model_res, config_path, checkpoint_path, engine,
                              guidance, steps, seed, enhance_mouth, enhance_region, out_res, input_type,
                              drive_folder, drive_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (name, str(video_path), str(audio_path), model_res, config_path, checkpoint_path, str(engine),
             float(guidance), int(steps), int(seed), int(bool(enhance_mouth)),
             str(enhance_region), str(out_res), "ai" if input_type == "ai" else "real",
             str(drive_folder) if drive_folder else None,
             str(drive_name) if drive_name else None),
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


def list_done_jobs(limit=200):
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM jobs WHERE status='done' AND output_path IS NOT NULL "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


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
