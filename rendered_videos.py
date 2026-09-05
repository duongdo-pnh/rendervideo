"""Shared "rendered videos" browsing/deletion logic (Desktop/Renders).

Reads straight off disk rather than the jobs DB — a rendered video survives
its job record being deleted from the queue (see database.py's delete_job
docstring), so the source of truth for "what videos exist" is the filesystem,
not a table. Both web_ui.py and the FastAPI backend need identical listing/
delete behavior.
"""
from datetime import datetime
from pathlib import Path

import database as db


def video_choices() -> dict:
    """{stem: absolute_path}, newest first by mtime."""
    d = db.RENDERS_DIR
    if not d.exists():
        return {}
    files = sorted(d.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    return {p.stem: str(p) for p in files}


def latest_video():
    """(name, path) of the newest rendered video, or (None, None) if there are none."""
    choices = video_choices()
    if not choices:
        return None, None
    name = next(iter(choices))
    return name, choices[name]


def list_rows() -> list:
    """[name, rendered_at, size_mb], newest first."""
    d = db.RENDERS_DIR
    if not d.exists():
        return []
    files = sorted(d.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    rows = []
    for p in files:
        st = p.stat()
        rows.append([
            p.stem,
            datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
            round(st.st_size / 1e6, 1),
        ])
    return rows


class VideoNotFound(Exception):
    pass


def delete_video(name: str) -> None:
    """Permanently delete a rendered video by name (filename stem). Disk only —
    never touches the jobs DB (see web_ui.py's do_delete_job for the job-record
    delete path, which intentionally keeps the video)."""
    path = video_choices().get(name)
    if not path or not Path(path).is_file():
        raise VideoNotFound(f"Không tìm thấy file cho '{name}' (có thể đã bị xóa từ trước).")
    Path(path).unlink()
