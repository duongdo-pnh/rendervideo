"""Cấu hình kết nối akali cho UI — lưu AKALI_URL/AKALI_WORKER_KEY/AKALI_WORKER_ID
thay vì bắt sửa tay .env. akali_bridge.py (chạy riêng, không phải backend này) mới
là thứ thật sự claim/heartbeat/complete job — xem doc_api/APi_RENDER_QUEUE.md.

Không có bước "kiểm tra kết nối" thật khi lưu — xem docstring của
akali_bridge.save_settings() cho lý do (claim() có tác dụng phụ thật).
"""
from fastapi import APIRouter, HTTPException

import akali_bridge
import database as db

from . import workers
from .models import AkaliCancelResponse, AkaliConfigRequest, AkaliStatus

router = APIRouter(prefix="/api/akali", tags=["akali"])


@router.get("/status", response_model=AkaliStatus)
def akali_status():
    return AkaliStatus(**akali_bridge.status())


@router.post("/batches/{akali_job_id}/cancel", response_model=AkaliCancelResponse)
def cancel_batch(akali_job_id: int):
    """Huỷ CẢ BATCH akali từ phía máy render (nhánh U, mục 4.2 của
    doc_api/APi_RENDER_QUEUE.md).

    Hai việc: đặt cờ để akali_bridge gọi `/cancel` báo akali (chỉ bridge mới giữ
    claim_token hợp lệ), và dừng ngay render job của batch để GPU khỏi chạy tiếp
    trong lúc chờ bridge phát hiện ở vòng poll kế.
    """
    marked = akali_bridge.request_batch_cancel(akali_job_id)
    if not marked:
        raise HTTPException(404, f"Không thấy batch akali #{akali_job_id} đang chạy.")
    stopped = []
    for _job_id, excel_stem in marked:
        stopped += db.request_cancel_by_drive_folder(excel_stem)
    return AkaliCancelResponse(
        cancelled_batches=[job_id for job_id, _ in marked],
        stopped_render_jobs=stopped,
        message=f"Đã huỷ batch #{akali_job_id}, dừng {len(stopped)} job render.",
    )


@router.post("/config", response_model=AkaliStatus)
def save_config(body: AkaliConfigRequest):
    akali_bridge.save_settings(body.akali_url, body.worker_key)
    # akali_bridge.py tự thoát ngay nếu URL/key trống lúc backend khởi động, và
    # không có gì tự khởi động lại nó sau đó — đảm bảo nó chạy ngay khi vừa lưu
    # đủ config, khỏi bắt người dùng đóng/mở lại app (xem workers.spawn_akali_bridge).
    if akali_bridge.bridge_enabled():
        workers.spawn_akali_bridge()
    return AkaliStatus(**akali_bridge.status())
