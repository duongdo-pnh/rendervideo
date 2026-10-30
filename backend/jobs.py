"""Render-queue job endpoints.

Wraps database.py directly (no Gradio dependency) — mirrors the behavior of
web_ui.py's add_to_queue / refresh_status / do_prioritize_job / do_delete_job /
do_clear, just returning JSON instead of gr.update() tuples.
"""
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

import akali_bridge
import database as db
import gpu_info
import job_files
import tts_db
from job_naming import KIND_ANSWER, build_name, slug_key
from render_job import OUT_RES

from .engines import unavailable_reason
from .models import (
    CancelAllResponse, CancelJobResponse, DeletedJobsResponse, EngineChoice, JobOut,
    JobSubmitResponse, PriorityChoice, ReorderRequest,
)

LATENTSYNC_BLOCKED_MESSAGE = (
    "LatentSync không khả dụng trên GPU RTX 3080/3080 Ti (hay gặp lỗi) — dùng MuseTalk."
)

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("", response_model=list[JobOut])
def list_jobs():
    """Rendering + queued (priority order) + history, newest first within each group —
    same ordering web_ui.py's _table_rows() uses for the queue-status table."""
    jobs = db.list_jobs(limit=200)
    rendering = [j for j in jobs if j["status"] == db.STATUS_RENDERING]
    queued = sorted(
        (j for j in jobs if j["status"] == db.STATUS_QUEUED),
        key=lambda j: (-int(j.get("priority") or 0), j.get("created_at") or "", j["id"]),
    )
    history = [j for j in jobs if j["status"] not in (db.STATUS_RENDERING, db.STATUS_QUEUED)]
    ordered = (*rendering, *queued, *history)
    batches = tts_db.batches_for_render_jobs([j["id"] for j in ordered])
    return [JobOut.from_row(j, batches.get(j["id"])) for j in ordered]


@router.get("/priority-choices", response_model=list[PriorityChoice])
def priority_choices():
    jobs = [j for j in db.list_jobs(limit=200) if j["status"] == db.STATUS_QUEUED]
    jobs.sort(key=lambda j: (-int(j.get("priority") or 0), j.get("created_at") or "", j["id"]))
    return [PriorityChoice(id=j["id"], label=f"#{j['id']} · {j['name']}") for j in jobs]


@router.get("/engine-choices", response_model=list[EngineChoice])
def engine_choices():
    choices = [EngineChoice(label="MuseTalk 1.5", value="musetalk")]
    if not gpu_info.latentsync_blocked():
        choices.append(EngineChoice(label="LatentSync", value="latentsync"))
    return choices


@router.post("", response_model=JobSubmitResponse, status_code=201)
async def submit_job(
    video: UploadFile,
    audio: UploadFile,
    product: str = Form(""),
    kind: str = Form(...),
    intent: Optional[str] = Form(None),
    other_key: Optional[str] = Form(None),
    model_res: str = Form("512"),
    guidance: float = Form(1.5),
    steps: int = Form(24),
    seed: int = Form(1247),
    enhance_mouth: bool = Form(True),
    enhance_region: str = Form("mouth"),
    out_res: str = Form("720"),
    input_type: str = Form("real"),
    engine: str = Form("musetalk"),
):
    if kind == KIND_ANSWER and intent == "ASK_OTHER" and not slug_key(other_key):
        raise HTTPException(422, "Chọn ASK_OTHER thì cần điền other_key, ví dụ: khautrang.")
    if out_res not in OUT_RES:
        raise HTTPException(422, f"out_res không hợp lệ (expected one of {list(OUT_RES)})")
    if engine == "latentsync" and gpu_info.latentsync_blocked():
        raise HTTPException(422, LATENTSYNC_BLOCKED_MESSAGE)

    name = build_name(product, kind, intent, other_key)
    v_dst, a_dst = job_files.write_upload_pair(
        video.filename or "video.mp4", await video.read(),
        audio.filename or "audio.wav", await audio.read(),
    )
    try:
        job_id = db.add_job(
            name, v_dst, a_dst, model_res, guidance, steps, seed,
            int(enhance_mouth), enhance_region, out_res, input_type, engine=engine,
        )
    except ValueError as e:
        raise HTTPException(422, str(e)) from None

    return JobSubmitResponse(
        job_id=job_id, name=name, engine=engine,
        message=f"Đã thêm job #{job_id} ('{name}') vào hàng đợi.",
    )


def fun5b_job_name(image_name, video_name):
    """Default Fun-5B job name: '<image stem>_<video stem>' (queue_worker adds _<id> on a clash)."""
    return f"{Path(image_name or 'anh').stem}_{Path(video_name or 'video').stem}"[:120]


_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}


@router.post("/animate", response_model=JobSubmitResponse, status_code=201)
async def submit_animate_job(
    image: UploadFile,
    video: UploadFile,
    background: Optional[UploadFile] = None,
    product: str = Form(""),
    kind: Optional[str] = Form(None),  # naming fields are optional: without them the name is <image>_<video>
    intent: Optional[str] = Form(None),
    other_key: Optional[str] = Form(None),
    preset: str = Form(db.FUN5B_DEFAULT_PRESET),
    seed: int = Form(db.FUN5B_DEFAULT_SEED),
    keep_background: bool = Form(True),
    auto_background: bool = Form(True),
    table_line: Optional[float] = Form(None),  # % of the height, 0/empty = automatic
    sharp_hands: bool = Form(False),
):
    """Fun-5B job (character image performs the driving video's motion; silent 720×1280 output)."""
    reason, _ = unavailable_reason()
    if reason:
        raise HTTPException(422, reason)
    if kind == KIND_ANSWER and intent == "ASK_OTHER" and not slug_key(other_key):
        raise HTTPException(422, "Chọn ASK_OTHER thì cần điền other_key, ví dụ: khautrang.")
    for upload, exts, label in ((image, _IMAGE_EXTS, "Ảnh nhân vật"), (video, _VIDEO_EXTS, "Video mẫu")):
        if Path(upload.filename or "").suffix.lower() not in exts:
            raise HTTPException(422, f"{label}: định dạng không hỗ trợ ({', '.join(sorted(exts))}).")
    if background is not None and background.filename and \
            Path(background.filename).suffix.lower() not in _IMAGE_EXTS:
        raise HTTPException(422, "Ảnh nền sạch: định dạng không hỗ trợ.")
    if preset not in db.FUN5B_PRESETS:
        raise HTTPException(422, f"preset không hợp lệ (expected one of {list(db.FUN5B_PRESETS)})")
    if table_line is not None and not 0 <= table_line < 100:
        raise HTTPException(422, "Mép bàn phải trong khoảng 0–99 (% chiều cao).")

    name = (build_name(product, kind, intent, other_key) if kind
            else fun5b_job_name(image.filename, video.filename))
    bg_bytes = await background.read() if background is not None and background.filename else None
    img_dst, vid_dst, bg_dst = job_files.write_fun5b_upload(
        image.filename, await image.read(), video.filename, await video.read(),
        background.filename if bg_bytes else None, bg_bytes,
    )
    try:
        job_id = db.add_fun5b_job(
            name, img_dst, vid_dst, preset=preset, seed=seed, keep_background=keep_background,
            auto_background=auto_background, table_line=(table_line / 100) if table_line else None,
            sharp_hands=sharp_hands, background_path=bg_dst,
        )
    except ValueError as e:
        job_files.cleanup_job_files({"video_path": str(vid_dst)})
        raise HTTPException(422, str(e)) from None

    return JobSubmitResponse(
        job_id=job_id, name=name, engine="fun5b",
        message=f"Đã thêm job Fun-5B #{job_id} ('{name}') vào hàng đợi.",
    )


@router.get("/{job_id}/ref-image")
def job_ref_image(job_id: int):
    """The character image of a Fun-5B job (queue thumbnail)."""
    row = db.get_job(job_id)
    path = Path((row or {}).get("ref_image_path") or "")
    if not row or row.get("engine") != "fun5b" or not path.is_file():
        raise HTTPException(404, "Không có ảnh nhân vật cho job này.")
    return FileResponse(path)


@router.post("/{job_id}/priority", response_model=JobOut)
def prioritize_job(job_id: int):
    try:
        row = db.prioritize_job(job_id)
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    return JobOut.from_row(row)


@router.post("/priority", response_model=list[int])
def reorder_jobs(body: ReorderRequest):
    try:
        return db.prioritize_jobs(body.job_ids)
    except ValueError as e:
        raise HTTPException(422, str(e)) from None


@router.post("/{job_id}/retry", response_model=JobOut)
def retry_job(job_id: int):
    try:
        row = db.retry_job(job_id)
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    return JobOut.from_row(row)


@router.post("/{job_id}/cancel", response_model=CancelJobResponse)
def cancel_job(job_id: int):
    """Hủy 1 job. 'queued' hủy ngay; 'rendering' chỉ đặt cờ — queue_worker.py (tiến trình
    riêng biệt) tự phát hiện ở vòng poll kế tiếp (tối đa vài giây) rồi mới thật sự dừng."""
    try:
        status_before = db.request_cancel(job_id)
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    message = (
        f"Đã hủy job #{job_id}." if status_before == db.STATUS_QUEUED
        else f"Đã gửi yêu cầu hủy job #{job_id} — sẽ dừng trong vài giây."
    )
    return CancelJobResponse(status_before=status_before, message=message)


@router.post("/cancel-all", response_model=CancelAllResponse)
def cancel_all_jobs():
    """Hủy TẤT CẢ job đang chờ + đặt cờ cho job đang render (worker tự dừng).

    Huỷ luôn cả batch akali đang chạy: "hủy hết" là ý định rõ ràng, không mơ hồ
    như huỷ lẻ 1 video. Không làm bước này thì akali_bridge vẫn tưởng batch đang
    chạy, vẫn heartbeat, và akali sẽ hiện batch đó là Xong/Lỗi thay vì Đã huỷ
    (xem doc_api/APi_RENDER_QUEUE.md nhánh U). Chỉ đặt cờ — bridge mới có
    claim_token để gọi /cancel báo akali.
    """
    n_queued, n_rendering = db.cancel_all_active()
    batches = akali_bridge.request_batch_cancel()
    total = n_queued + n_rendering
    if total == 0:
        message = "Không có job đang chờ/đang render để hủy."
    else:
        parts = []
        if n_queued:
            parts.append(f"{n_queued} đang chờ")
        if n_rendering:
            parts.append(f"{n_rendering} đang render (sẽ dừng trong vài giây)")
        message = f"Đã hủy {' + '.join(parts)}."
    if batches:
        message += f" Đã huỷ {len(batches)} batch akali, đang báo về akali."
    return CancelAllResponse(cancelled_queued=n_queued, flagged_rendering=n_rendering, message=message)


@router.delete("/{job_id}", response_model=DeletedJobsResponse)
def delete_job(job_id: int):
    try:
        row = db.delete_job(job_id)
    except ValueError as e:  # job đang render
        raise HTTPException(409, str(e)) from None
    if not row:
        raise HTTPException(404, f"Không thấy job #{job_id}.")
    job_files.cleanup_job_files(row)
    return DeletedJobsResponse(deleted=1, message=f"Đã xóa job #{job_id} ('{row['name']}').")


@router.post("/clear/{status}", response_model=DeletedJobsResponse)
def clear_jobs(status: str):
    try:
        rows = db.clear_jobs(status)
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    for r in rows:
        job_files.cleanup_job_files(r)
    return DeletedJobsResponse(deleted=len(rows), message=f"Đã xóa {len(rows)} job.")
