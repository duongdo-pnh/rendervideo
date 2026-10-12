"""Excel batch import + TTS queue endpoints.

Mirrors web_ui.py's "📥 Import Excel" tab (preview_excel/submit_excel/
export_excel_results) and its "Trạng thái TTS" section (tts_status_md/
requeue_dead_letter) — same underlying excel_import.py/tts_db.py calls, JSON in
and out instead of Gradio's gr.update() tuples.

Preview -> submit state: Gradio keeps the parsed row data in a client-side
gr.State component and round-trips it back on Submit. The API does the exact
same thing statelessly — /preview returns an opaque `state` blob, /submit
expects it back unmodified — rather than caching parsed rows server-side.
"""
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

import excel_import
import gpu_info
import job_files
import tts_db
from latentsync.tts.factory import available_providers, list_voices

from .models import (
    ExcelPreviewResponse, ExcelSubmitRequest, ExcelSubmitResponse,
    RequeueResponse, TtsRetryRequest, TtsStatusResponse, VoiceChoice,
)

router = APIRouter(prefix="/api/excel", tags=["excel"])
tts_router = APIRouter(prefix="/api/tts", tags=["tts"])


@router.get("/template")
def download_template():
    path = excel_import.make_template("template.xlsx")
    return FileResponse(
        path, filename="template.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@router.post("/preview", response_model=ExcelPreviewResponse)
async def preview_excel(
    excel_file: UploadFile = File(...),
    avatar_videos: list[UploadFile] = File(default=[]),
    default_provider: str | None = Form(None),
    shopee_item_id: str | None = Form(None),
):
    shopee = (shopee_item_id or "").strip() or None
    stable_excel = job_files.write_excel_upload(excel_file.filename, await excel_file.read())
    # Mỗi avatar coi như gắn với 1 sản phẩm (tên file = tên sản phẩm) — excel_import.
    # _resolve_avatar() khớp theo tên; chỉ 1 avatar thì mọi dòng dùng chung nó, y hệt
    # "video mặc định" kiểu cũ.
    default_videos = [
        {"name": Path(f.filename).stem, "path": job_files.write_default_video(f.filename, await f.read())}
        for f in avatar_videos if f.filename
    ]

    rows, errors = excel_import.process_excel(stable_excel, default_videos, default_provider, shopee)
    summary = f"Tổng: {len(rows) + len(errors)} | Sẵn sàng: {len(rows)} | Lỗi: {len(errors)}"
    return ExcelPreviewResponse(
        rows=rows, errors=errors, summary=summary,
        state={"rows": rows, "shopee": shopee, "excel_path": stable_excel},
    )


@router.post("/submit", response_model=ExcelSubmitResponse)
def submit_excel(body: ExcelSubmitRequest):
    rows = body.state.get("rows")
    if not rows:
        raise HTTPException(422, "Chưa có dòng sẵn sàng — gọi /preview trước.")
    if body.engine == "latentsync" and gpu_info.latentsync_blocked():
        raise HTTPException(422, "LatentSync không khả dụng trên GPU RTX 3080/3080 Ti (hay gặp lỗi) — dùng MuseTalk.")
    render_config = dict(
        model_res=body.model_res, out_res=body.out_res,
        enhance_mouth=int(body.enhance_mouth), enhance_region=body.enhance_region,
        guidance=body.guidance, steps=body.steps, seed=body.seed,
        input_type=body.input_type, engine=body.engine,
    )
    enqueued, warnings, batch_id, skipped = excel_import.submit_jobs(
        rows, shopee_item_id=body.state.get("shopee"), excel_path=body.state.get("excel_path"),
        render_config=render_config, dedup=body.dedup,
    )
    return ExcelSubmitResponse(
        batch_id=batch_id, enqueued=len(enqueued), skipped=len(skipped), warnings=len(warnings),
        skipped_detail=skipped, warnings_detail=warnings,
        message=f"Đã đưa {len(enqueued)} dòng vào TTS queue (batch {batch_id}), bỏ qua {len(skipped)} dòng trùng.",
    )


@router.get("/export/{batch_id}")
def export_excel_results(batch_id: str):
    try:
        out, _filled, _total = excel_import.export_results(batch_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from None
    return FileResponse(
        out, filename=Path(out).name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


_TTS_LABEL = {"pending": "⏳ chờ", "submitting": "🔄 đang chạy", "retry_wait": "🔁 chờ retry",
              "done": "✅ xong", "failed_retryable": "🟠 dead-letter", "failed_permanent": "❌ lỗi cứng"}


@tts_router.get("/status", response_model=TtsStatusResponse)
def tts_status():
    counts = tts_db.status_counts()
    dead = tts_db.list_jobs(limit=5, status="failed_retryable")
    jobs = tts_db.list_jobs(limit=200)
    batch_names = tts_db.batch_filenames([j["batch_id"] for j in jobs])
    return TtsStatusResponse(
        counts={_TTS_LABEL.get(k, k): v for k, v in counts.items()},
        dead_letter=[{"row": d["excel_row"], "error": str(d["last_error"])[:200]} for d in dead],
        jobs=[
            {
                "id": j["id"],
                "row": j["excel_row"],
                "product": j["product"],
                "text": j["text"],
                "status": j["status"],
                "status_label": _TTS_LABEL.get(j["status"], j["status"]),
                "error": str(j["last_error"])[:300] if j["last_error"] else None,
                "updated_at": j["updated_at"],
                "batch_id": j["batch_id"],
                "excel_file": batch_names.get(j["batch_id"]),
            }
            for j in jobs
        ],
    )


@tts_router.post("/requeue-dead-letter", response_model=RequeueResponse)
def requeue_dead_letter():
    n = tts_db.requeue_dead_letter()
    return RequeueResponse(requeued=n, message=f"Đưa lại {n} dòng dead-letter vào queue." if n else "Không có dòng dead-letter.")


@tts_router.post("/requeue-failed-permanent", response_model=RequeueResponse)
def requeue_failed_permanent(body: TtsRetryRequest):
    n = tts_db.requeue_failed_permanent(body.tts_job_ids)
    return RequeueResponse(requeued=n, message=f"Đưa lại {n} dòng lỗi cứng vào queue." if n else "Không có dòng nào được chọn.")


@tts_router.post("/requeue-by-ids", response_model=RequeueResponse)
def requeue_by_ids(body: TtsRetryRequest):
    n = tts_db.requeue_by_ids(body.tts_job_ids)
    return RequeueResponse(requeued=n, message=f"Đưa lại {n} dòng vào queue." if n else "Không có dòng nào được chọn.")


@tts_router.get("/providers", response_model=list[dict])
def tts_providers():
    return available_providers()


@tts_router.get("/providers/{provider}/voices", response_model=list[VoiceChoice])
def tts_voices(provider: str):
    voices = list_voices(provider)
    out = []
    for v in voices:
        if isinstance(v, (tuple, list)) and len(v) == 2:
            out.append(VoiceChoice(label=v[0], value=v[1]))
        elif v:
            out.append(VoiceChoice(label=str(v), value=str(v)))
    return out
