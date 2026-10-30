"""Pydantic response/request models for the render-queue API.

Field names mirror database.py's `jobs` table columns 1:1 so JobOut is a
near-direct serialization of a DB row — see database.py's init_db() for the
authoritative schema.
"""
from typing import Any, Optional

from pydantic import BaseModel


class JobOut(BaseModel):
    id: int
    name: str
    status: str
    engine: str
    model_res: str
    out_res: str
    input_type: str
    guidance: float
    steps: int
    seed: int
    enhance_mouth: bool
    enhance_region: str
    priority: int
    retries: int
    error: Optional[str] = None
    output_path: Optional[str] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    drive_link: Optional[str] = None
    drive_error: Optional[str] = None
    drive_folder: Optional[str] = None
    batch_id: Optional[str] = None
    excel_file: Optional[str] = None
    progress: Optional[float] = None
    # engine == "fun5b" only
    ref_image_path: Optional[str] = None
    fun5b_preset: Optional[str] = None
    keep_background: Optional[bool] = None
    auto_background: Optional[bool] = None
    table_line: Optional[float] = None
    sharp_hands: Optional[bool] = None
    has_background: Optional[bool] = None

    @classmethod
    def from_row(cls, row: dict, batch: Optional[dict] = None) -> "JobOut":
        fun5b = row["engine"] == "fun5b"
        return cls(
            progress=row.get("progress"),
            ref_image_path=row.get("ref_image_path") if fun5b else None,
            fun5b_preset=row.get("fun5b_preset") if fun5b else None,
            keep_background=bool(row.get("keep_background")) if fun5b else None,
            auto_background=bool(row.get("auto_background")) if fun5b else None,
            table_line=row.get("table_line") if fun5b else None,
            sharp_hands=bool(row.get("sharp_hands")) if fun5b else None,
            has_background=bool(row.get("background_path")) if fun5b else None,
            id=row["id"], name=row["name"], status=row["status"], engine=row["engine"],
            model_res=row["model_res"], out_res=row["out_res"], input_type=row["input_type"],
            guidance=row["guidance"], steps=row["steps"], seed=row["seed"],
            enhance_mouth=bool(row["enhance_mouth"]), enhance_region=row["enhance_region"],
            priority=row["priority"] or 0, retries=row["retries"], error=row["error"],
            output_path=row["output_path"], created_at=row["created_at"],
            started_at=row["started_at"], finished_at=row["finished_at"],
            drive_link=row["drive_link"], drive_error=row["drive_error"],
            drive_folder=row["drive_folder"],
            batch_id=(batch or {}).get("batch_id"), excel_file=(batch or {}).get("excel_file"),
        )


class JobSubmitResponse(BaseModel):
    job_id: int
    name: str
    engine: str
    message: str


class Fun5bPreset(BaseModel):
    key: str
    label: str
    steps: int
    note: str
    seconds_per_10s: int  # measured render time of 10s of driving video on an RTX 3090


class Fun5bDefaults(BaseModel):
    preset: str
    seed: int
    keep_background: bool
    auto_background: bool
    sharp_hands: bool


class Fun5bInstallStatus(BaseModel):
    state: str  # installed | running | failed | skipped | not_installed  (fun5b_setup.status())
    step: int
    total: int
    label: str
    message: Optional[str] = None
    downloaded_gb: Optional[float] = None
    models_gb: float


class Fun5bInfo(BaseModel):
    available: bool
    reason: Optional[str] = None
    missing: list[str] = []
    plate_available: bool
    presets: list[Fun5bPreset]
    defaults: Fun5bDefaults
    install: Fun5bInstallStatus


class Fun5bServerStatus(BaseModel):
    running: bool
    loaded: bool = False
    busy: bool = False
    job_id: Optional[int] = None
    vram_mb: Optional[int] = None
    gpu_free_mb: Optional[int] = None
    gpu_total_mb: Optional[int] = None


class Fun5bStopResponse(BaseModel):
    stopped: bool
    message: str


class Fun5bPlate(BaseModel):
    id: str
    source_name: Optional[str] = None
    created_at: Optional[str] = None
    last_used: Optional[str] = None
    size_kb: int
    has_source: bool


class Fun5bPlateLookup(BaseModel):
    id: str
    cached: bool


class PriorityChoice(BaseModel):
    id: int
    label: str


class EngineChoice(BaseModel):
    label: str
    value: str


class ReorderRequest(BaseModel):
    job_ids: list[int]


class DeletedJobsResponse(BaseModel):
    deleted: int
    message: str


class CancelJobResponse(BaseModel):
    status_before: str  # 'queued' (huỷ ngay) hoặc 'rendering' (worker sẽ tự dừng trong vài giây)
    message: str


class CancelAllResponse(BaseModel):
    cancelled_queued: int
    flagged_rendering: int
    message: str


class VideoOut(BaseModel):
    name: str
    rendered_at: str
    size_mb: float


class ErrorResponse(BaseModel):
    detail: str


class ExcelPreviewResponse(BaseModel):
    rows: list[dict]      # ready rows, raw from excel_import.process_excel — opaque to the client
    errors: list[dict]    # [{row, error}]
    summary: str
    state: dict[str, Any]  # {rows, shopee, excel_path} — round-trip this back to /submit unmodified


class ExcelSubmitRequest(BaseModel):
    state: dict[str, Any]
    model_res: str = "512"
    out_res: str = "720"
    enhance_mouth: bool = True
    guidance: float = 1.5
    steps: int = 24
    seed: int = 1247
    enhance_region: str = "mouth"
    input_type: str = "real"
    engine: str = "musetalk"
    dedup: bool = True


class ExcelSubmitResponse(BaseModel):
    batch_id: str
    enqueued: int
    skipped: int
    warnings: int
    skipped_detail: list[dict] = []
    warnings_detail: list[dict] = []
    message: str


class TtsStatusResponse(BaseModel):
    counts: dict[str, int]
    dead_letter: list[dict]
    jobs: list[dict] = []


class RequeueResponse(BaseModel):
    requeued: int
    message: str


class TtsRetryRequest(BaseModel):
    tts_job_ids: list[int]


class VoiceChoice(BaseModel):
    label: str
    value: str


class AvatarCacheEntry(BaseModel):
    video_name: str
    source_name: Optional[str] = None
    duration_secs: Optional[float] = None
    avatar_id: str
    size_bytes: int
    built_at: str
    status: str  # LIVE | STALE | TEST | MISSING


class ClearStaleResponse(BaseModel):
    removed: list[str]
    freed_bytes: int
    message: str


class SampleAudioChoice(BaseModel):
    label: str
    path: str


class LivestreamEnqueueResponse(BaseModel):
    status: dict
    message: str


class LivestreamEnqueueSampleRequest(BaseModel):
    sample_path: str
    request_id: str = ""
    priority: int = 0
    interrupt: bool = False


class TtsConfigOut(BaseModel):
    default_provider: str
    values: dict[str, str]
    providers: list[dict]


class TtsConfigSaveRequest(BaseModel):
    default_provider: str
    values: dict[str, str]


class TtsConfigSchema(BaseModel):
    common_fields: list[list]   # [(key, label, is_secret), ...] as-is from tts_config.py
    provider_fields: dict[str, list[list]]
    provider_labels: dict[str, str]
    curated_voices: dict[str, list[VoiceChoice]] = {}


class VbeeConnectRequest(BaseModel):
    app_id: str
    token: str


class VbeeTestRequest(BaseModel):
    app_id: str
    token: str
    voice: str = ""
    text: str


class AusynclabConnectRequest(BaseModel):
    api_key: str


class AusynclabTestRequest(BaseModel):
    api_key: str
    voice: str = ""
    text: str


class AutovoiceConnectRequest(BaseModel):
    api_key: str
    url: str = ""
    voices_url: str = ""


class AutovoiceTestRequest(BaseModel):
    api_key: str
    voice: str = ""
    url: str = ""
    speed: str = ""
    text: str


class VoiceConnectResponse(BaseModel):
    voices: list[VoiceChoice]
    message: str


class DriveStatus(BaseModel):
    enabled: bool
    configured: bool
    folder_id: Optional[str] = None
    folder_name: Optional[str] = None
    folder_link: Optional[str] = None
    error: Optional[str] = None


class DriveFile(BaseModel):
    id: str
    name: str
    mime_type: str
    web_view_link: Optional[str] = None
    modified_time: Optional[str] = None
    size: Optional[int] = None
    is_folder: bool


class DriveFilesResponse(BaseModel):
    folder_id: str
    folder_name: str
    folder_link: Optional[str] = None
    files: list[DriveFile]


class DriveReauthStatus(BaseModel):
    status: str  # idle | pending | success | error
    message: Optional[str] = None


class DriveConfigRequest(BaseModel):
    folder_id: str
    enabled: bool = True


class DashboardStatus(BaseModel):
    configured: bool
    dashboard_url: Optional[str] = None
    ingest_token: Optional[str] = None
    node_name: Optional[str] = None
    error: Optional[str] = None


class DashboardConfigRequest(BaseModel):
    # Không có dashboard_url: URL cố định trong dashboard_reporter._DEFAULT_DASHBOARD_URL.
    ingest_token: str = ""
    node_name: str = ""


class AkaliStatus(BaseModel):
    configured: bool
    akali_url: Optional[str] = None
    worker_key: Optional[str] = None
    worker_id: Optional[str] = None


class AkaliConfigRequest(BaseModel):
    akali_url: str = ""
    worker_key: str = ""


class AkaliCancelResponse(BaseModel):
    cancelled_batches: list[int]
    stopped_render_jobs: list[int]
    message: str
