"""Fun-5B engine endpoints: availability, the resident render server, saved clean plates.

Job creation is POST /api/jobs/animate (backend/jobs.py); this router is everything around it.
The engine itself runs in engines/Fun5B/.venv — nothing here imports fun5b's heavy modules,
only fun5b_adapter (paths, socket client) and fun5b.plate_key (stdlib only).
"""
import io
import json
import re
from datetime import datetime
from pathlib import Path

import numpy as np
from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import FileResponse
from PIL import Image, ImageOps

import database as db
import fun5b_adapter
import fun5b_setup
from fun5b import plate_key

from .models import (
    DeletedJobsResponse, Fun5bDefaults, Fun5bInfo, Fun5bInstallStatus, Fun5bPlate, Fun5bPlateLookup, Fun5bPreset,
    Fun5bServerStatus, Fun5bStopResponse,
)

router = APIRouter(prefix="/api/engines/fun5b", tags=["fun5b"])

# Measured on the RTX 3090 (30s clips, 14 steps ~27 min); the other presets scale with their steps
# (natural_light also runs CFG, two passes per step).
PRESETS = [
    Fun5bPreset(key="fast", label="Nhanh", steps=8, seconds_per_10s=330,
                note="Nhanh nhất; ánh sáng kiểu studio (da bóng, tương phản)"),
    Fun5bPreset(key="balanced", label="Cân bằng", steps=12, seconds_per_10s=470,
                note="Ánh sáng studio nhẹ hơn"),
    Fun5bPreset(key="natural", label="Tự nhiên", steps=14, seconds_per_10s=540,
                note="Mặc định: giữ ánh sáng của ảnh gốc"),
    Fun5bPreset(key="natural_light", label="Ánh sáng tự nhiên (chậm)", steps=25, seconds_per_10s=1250,
                note="Sát ảnh gốc nhất, chậm hơn ~2.3 lần"),
]
_PLATE_ID = re.compile(r"^[0-9a-f]{32}$")


def unavailable_reason():
    """Why Fun-5B cannot run on this machine (None = it can), and the missing files."""
    return fun5b_adapter.unavailable_reason()


@router.get("", response_model=Fun5bInfo)
def info():
    reason, missing = unavailable_reason()
    return Fun5bInfo(
        available=reason is None, reason=reason, missing=missing,
        plate_available=fun5b_adapter.plate_available(), presets=PRESETS,
        defaults=Fun5bDefaults(preset=db.FUN5B_DEFAULT_PRESET, seed=db.FUN5B_DEFAULT_SEED,
                               keep_background=True, auto_background=True, sharp_hands=False),
        install=Fun5bInstallStatus(**fun5b_setup.status()),
    )


@router.post("/install", response_model=Fun5bInstallStatus)
def install():
    """(Re)start the background install (install/setup_fun5b.sh); it also starts by itself at app start."""
    if fun5b_setup.running():
        return Fun5bInstallStatus(**fun5b_setup.status())
    if fun5b_setup.installed():
        raise HTTPException(409, "Fun-5B đã được cài đầy đủ.")
    code, reason = fun5b_setup.preflight()
    if code != 0:
        raise HTTPException(422, reason)
    return Fun5bInstallStatus(**fun5b_setup.start())


@router.get("/server", response_model=Fun5bServerStatus)
def server():
    status = fun5b_adapter.server_status()
    if status is None:
        return Fun5bServerStatus(running=False)
    return Fun5bServerStatus(running=True, **{k: status.get(k) for k in (
        "loaded", "busy", "job_id", "vram_mb", "gpu_free_mb", "gpu_total_mb")})


@router.post("/server/stop", response_model=Fun5bStopResponse)
def stop_server():
    status = fun5b_adapter.server_status()
    if status and status.get("busy"):
        raise HTTPException(409, f"Server đang render job #{status.get('job_id')} — huỷ job trước.")
    if not fun5b_adapter.stop_server():
        raise HTTPException(409, "Đang có job Fun-5B chạy — huỷ job trước.")
    return Fun5bStopResponse(stopped=True, message="Đã tắt server Fun-5B (model vẫn giữ trên ổ đĩa).")


def _plate_path(plate_id, suffix=".png"):
    if not _PLATE_ID.match(plate_id):
        raise HTTPException(404, "Không tìm thấy nền này.")
    return fun5b_adapter.PLATE_DIR / f"{plate_id}{suffix}"


def _meta(plate_id):
    try:
        return json.loads(_plate_path(plate_id, ".json").read_text())
    except (OSError, ValueError):
        return {}


@router.get("/plates", response_model=list[Fun5bPlate])
def list_plates():
    plates = []
    for path in fun5b_adapter.PLATE_DIR.glob("*.png") if fun5b_adapter.PLATE_DIR.is_dir() else []:
        if not _PLATE_ID.match(path.stem):
            continue
        meta = _meta(path.stem)
        fallback = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        plates.append(Fun5bPlate(
            id=path.stem, source_name=meta.get("source_name"),
            created_at=meta.get("created_at") or fallback, last_used=meta.get("last_used") or fallback,
            size_kb=round(path.stat().st_size / 1024),
            has_source=_plate_path(path.stem, "_source.jpg").is_file(),
        ))
    plates.sort(key=lambda p: p.last_used or "", reverse=True)
    return plates


@router.get("/plates/{plate_id}/image")
def plate_image(plate_id: str, kind: str = "plate"):
    path = _plate_path(plate_id, "_source.jpg" if kind == "source" else ".png")
    if not path.is_file():
        raise HTTPException(404, "Không có ảnh này.")
    return FileResponse(path, media_type="image/jpeg" if kind == "source" else "image/png")


@router.delete("/plates/{plate_id}", response_model=DeletedJobsResponse)
def delete_plate(plate_id: str):
    paths = [_plate_path(plate_id, s) for s in (".png", "_source.jpg", ".json")]
    if not paths[0].is_file():
        raise HTTPException(404, "Không tìm thấy nền này.")
    for p in paths:
        p.unlink(missing_ok=True)
    return DeletedJobsResponse(deleted=1, message="Đã xoá nền đã lưu. Lần sau dùng ảnh này sẽ vẽ lại (~7 phút).")


def fitted_reference_key(image_bytes):
    """plate_key of an uploaded image, fitted exactly as fun5b_render_server does."""
    try:
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception:
        raise HTTPException(422, "Không đọc được ảnh nhân vật.") from None
    fitted = np.asarray(ImageOps.fit(image, (plate_key.RENDER_W, plate_key.RENDER_H), Image.LANCZOS))
    return plate_key.cache_key(fitted.tobytes())


@router.post("/plates/lookup", response_model=Fun5bPlateLookup)
async def lookup_plate(image: UploadFile):
    """Whether this character image already has a saved clean background (no FireRed wait)."""
    key = fitted_reference_key(await image.read())
    return Fun5bPlateLookup(id=key, cached=Path(_plate_path(key)).is_file())
