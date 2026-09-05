"""Rendered-video browse/preview/delete endpoints — reads Desktop/Renders directly
off disk, same source of truth web_ui.py's "📺 Video đã render" tab uses.
"""
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

import rendered_videos
from .models import DeletedJobsResponse, VideoOut

router = APIRouter(prefix="/api/videos", tags=["videos"])


@router.get("", response_model=list[VideoOut])
def list_videos():
    return [
        VideoOut(name=name, rendered_at=rendered_at, size_mb=size_mb)
        for name, rendered_at, size_mb in rendered_videos.list_rows()
    ]


def _resolve(name: str) -> str:
    path = rendered_videos.video_choices().get(name)
    if not path:
        raise HTTPException(404, f"Không tìm thấy video '{name}'.")
    return path


@router.get("/{name}/stream")
def stream_video(name: str):
    """Inline playback (Range-request friendly via FileResponse)."""
    path = _resolve(name)
    return FileResponse(path, media_type="video/mp4")


@router.get("/{name}/download")
def download_video(name: str):
    path = _resolve(name)
    filename = f"{name}.mp4"
    return FileResponse(
        path, media_type="video/mp4", filename=filename,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@router.delete("/{name}", response_model=DeletedJobsResponse)
def delete_video(name: str):
    try:
        rendered_videos.delete_video(name)
    except rendered_videos.VideoNotFound as e:
        raise HTTPException(404, str(e)) from None
    except OSError as e:
        raise HTTPException(500, f"Không xóa được '{name}': {e}") from None
    return DeletedJobsResponse(deleted=1, message=f"Đã xóa '{name}'.")
