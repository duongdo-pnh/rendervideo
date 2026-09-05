"""MuseTalk avatar-cache browse/cleanup endpoints — mirrors web_ui.py's
"🗄️ Avatar Cache" tab. See avatar_cache.py for the shared LIVE/STALE/TEST logic.
"""
from fastapi import APIRouter, HTTPException

import avatar_cache
from .models import AvatarCacheEntry, ClearStaleResponse, DeletedJobsResponse

router = APIRouter(prefix="/api/avatar-cache", tags=["avatar-cache"])


@router.get("", response_model=list[AvatarCacheEntry])
def list_avatar_cache():
    return [AvatarCacheEntry(**e) for e in avatar_cache.list_entries()]


@router.post("/clear-stale", response_model=ClearStaleResponse)
def clear_stale_avatar_cache():
    removed, freed_bytes = avatar_cache.clear_stale()
    message = (f"Đã xóa {len(removed)} bản cache STALE, giải phóng ~{freed_bytes / 1e9:.1f} GB."
               if removed else "Không có bản cache STALE nào để xóa.")
    return ClearStaleResponse(removed=removed, freed_bytes=freed_bytes, message=message)


@router.delete("/{avatar_id}", response_model=DeletedJobsResponse)
def delete_avatar_cache_entry(avatar_id: str):
    try:
        freed_bytes = avatar_cache.delete_entry(avatar_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from None
    return DeletedJobsResponse(deleted=1, message=f"Đã xóa cache '{avatar_id}' (~{freed_bytes / 1e9:.2f} GB).")
