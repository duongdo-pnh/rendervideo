"""Google Drive browse + re-auth endpoints — new UI capability, no Gradio
equivalent existed (google_drive_upload.py was CLI-only: --auth/--check).

Re-auth runs google_drive_upload.run_oauth_flow() in a background thread
(it's a blocking call — opens a browser, waits on a local HTTP server for the
OAuth redirect) and exposes its progress via polling, same pattern as
musetalk avatar-prep polling elsewhere in this API.
"""
import threading

from fastapi import APIRouter, HTTPException, Query

import google_drive_upload as gdrive

from .models import DriveConfigRequest, DriveFile, DriveFilesResponse, DriveReauthStatus, DriveStatus

router = APIRouter(prefix="/api/drive", tags=["drive"])

_reauth_lock = threading.Lock()
_reauth_state: dict = {"status": "idle", "message": None}


def _status() -> DriveStatus:
    enabled = gdrive.drive_enabled()
    folder_id = gdrive.default_folder_id()
    if not folder_id:
        return DriveStatus(enabled=enabled, configured=False, error="Chưa điền ID thư mục Drive — dán link/ID rồi Lưu bên dưới.")
    try:
        info = gdrive.folder_info(folder_id)
    except gdrive.DriveConfigError as e:
        return DriveStatus(enabled=enabled, configured=False, folder_id=folder_id, error=str(e))
    except Exception as e:
        return DriveStatus(enabled=enabled, configured=False, folder_id=folder_id, error=f"Lỗi Drive API: {e}")
    return DriveStatus(
        enabled=enabled, configured=True, folder_id=info["id"],
        folder_name=info["name"], folder_link=info.get("web_view_link"),
    )


@router.get("/status", response_model=DriveStatus)
def drive_status():
    return _status()


@router.post("/config", response_model=DriveStatus)
def save_config(body: DriveConfigRequest):
    gdrive.save_settings(folder_id=body.folder_id, enabled=body.enabled)
    return _status()


@router.get("/files", response_model=DriveFilesResponse)
def drive_files(folder_id: str | None = Query(None)):
    status = _status()
    if not status.configured:
        raise HTTPException(409, status.error or "Drive chưa được cấu hình.")
    target_id = folder_id or status.folder_id
    try:
        if folder_id and folder_id != status.folder_id:
            info = gdrive.folder_info(folder_id)
        else:
            info = {"id": status.folder_id, "name": status.folder_name, "web_view_link": status.folder_link}
        raw = gdrive.list_files(target_id)
    except gdrive.DriveConfigError as e:
        raise HTTPException(409, str(e)) from None
    except Exception as e:
        raise HTTPException(502, f"Lỗi Drive API: {e}") from None
    return DriveFilesResponse(
        folder_id=info["id"], folder_name=info["name"], folder_link=info.get("web_view_link"),
        files=[DriveFile(**f) for f in raw],
    )


REAUTH_TIMEOUT_SECONDS = 60


def _run_reauth():
    try:
        gdrive.run_oauth_flow(open_browser=True, timeout_seconds=REAUTH_TIMEOUT_SECONDS)
        with _reauth_lock:
            _reauth_state.update(status="success", message="Đã lấy token mới, Drive đã sẵn sàng.")
    except Exception as e:
        with _reauth_lock:
            _reauth_state.update(status="error", message=str(e))


@router.post("/reauth", response_model=DriveReauthStatus)
def start_reauth():
    with _reauth_lock:
        if _reauth_state["status"] == "pending":
            raise HTTPException(409, "Đang có một phiên xác thực khác chạy — hoàn tất trên trình duyệt trước.")
        _reauth_state.update(
            status="pending",
            message=f"Đã mở trình duyệt — hoàn tất đăng nhập & đồng ý quyền truy cập trong {REAUTH_TIMEOUT_SECONDS}s.",
        )
    threading.Thread(target=_run_reauth, daemon=True).start()
    return DriveReauthStatus(**_reauth_state)


@router.get("/reauth/status", response_model=DriveReauthStatus)
def reauth_status():
    with _reauth_lock:
        return DriveReauthStatus(**_reauth_state)
