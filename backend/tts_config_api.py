"""TTS provider configuration endpoints — mirrors web_ui.py's "⚙️ Cấu hình TTS" tab.

tts_config.py is already Gradio-independent (reads/writes .env directly), so
this is a thin wrapper, same pattern as excel.py's /api/tts/* status endpoints.
The per-provider connect/test endpoints intentionally take credentials in the
request body rather than reading .env — same as the Gradio tab, this lets a
user try credentials before saving them.
"""
import job_files
import tts_config
import tts_errors
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from .models import (
    AusynclabConnectRequest, AusynclabTestRequest, AutovoiceConnectRequest,
    AutovoiceTestRequest, TtsConfigOut, TtsConfigSaveRequest, TtsConfigSchema,
    VbeeConnectRequest, VbeeTestRequest, VoiceChoice, VoiceConnectResponse,
)

router = APIRouter(prefix="/api/tts-config", tags=["tts-config"])


@router.get("/schema", response_model=TtsConfigSchema)
def schema():
    from latentsync.tts.ausynclab import AusynclabTTS
    from latentsync.tts.vbee import VbeeTTS

    return TtsConfigSchema(
        common_fields=[list(f) for f in tts_config.COMMON_FIELDS],
        provider_fields={p: [list(f) for f in fields] for p, fields in tts_config.PROVIDER_FIELDS.items()},
        provider_labels=tts_config.PROVIDER_LABELS,
        # 4 giọng cố định mỗi provider, không cần credentials/kết nối mạng —
        # mirrors web_ui.py's static VbeeTTS.CURATED_VOICES / AusynclabTTS.CURATED_VOICES dropdowns.
        curated_voices={
            "vbee": [VoiceChoice(label=label, value=value) for label, value in VbeeTTS.CURATED_VOICES],
            "ausynclab": [VoiceChoice(label=label, value=value) for label, value in AusynclabTTS.CURATED_VOICES],
        },
    )


@router.get("", response_model=TtsConfigOut)
def get_config():
    from latentsync.tts.factory import available_providers
    return TtsConfigOut(
        default_provider=tts_config.current_default_provider(),
        values=tts_config.current_values(),
        providers=available_providers(),
    )


@router.post("", response_model=TtsConfigOut)
def save_config(body: TtsConfigSaveRequest):
    providers = tts_config.save_config(body.values, body.default_provider)
    return TtsConfigOut(
        default_provider=tts_config.current_default_provider(),
        values=tts_config.current_values(),
        providers=providers,
    )


def _voice_choices(raw) -> list[VoiceChoice]:
    out = []
    for v in raw:
        if isinstance(v, (tuple, list)) and len(v) == 2:
            out.append(VoiceChoice(label=v[0], value=v[1]))
        elif v:
            out.append(VoiceChoice(label=str(v), value=str(v)))
    return out


@router.post("/vbee/connect", response_model=VoiceConnectResponse)
def vbee_connect(body: VbeeConnectRequest):
    from latentsync.tts.vbee import VbeeTTS
    try:
        voices = VbeeTTS(app_id=body.app_id, token=body.token).fetch_voices()
    except Exception as e:
        raise HTTPException(502, tts_errors.describe(e, "vbee")) from None
    codes = [v["code"] for v in voices if v.get("code")]
    return VoiceConnectResponse(voices=_voice_choices(codes), message=f"Đã tải {len(codes)} giọng Vbee.")


@router.post("/vbee/test")
def vbee_test(body: VbeeTestRequest):
    from latentsync.tts.vbee import VbeeTTS
    if not body.text.strip():
        raise HTTPException(422, "Nhập câu cần đọc thử.")
    out = str(job_files.UPLOADS_DIR / "vbee_test.wav")
    try:
        VbeeTTS(app_id=body.app_id, token=body.token, default_voice=body.voice or None).synthesize(
            body.text, out, body.voice or None)
    except Exception as e:
        raise HTTPException(502, tts_errors.describe(e, "vbee")) from None
    return FileResponse(out, media_type="audio/wav")


@router.post("/ausynclab/connect", response_model=VoiceConnectResponse)
def ausynclab_connect(body: AusynclabConnectRequest):
    from latentsync.tts.ausynclab import AusynclabTTS
    try:
        voices = AusynclabTTS(api_key=body.api_key).fetch_voices()
    except Exception as e:
        raise HTTPException(502, tts_errors.describe(e, "ausynclab")) from None
    choices = []
    for v in voices:
        if not v.get("code"):
            continue
        meta = " · ".join(x for x in (v.get("language_code"), v.get("gender"), v.get("use_case")) if x)
        label = f"{v.get('name') or 'voice'}" + (f" ({meta})" if meta else "") + f" · #{v['code']}"
        choices.append(VoiceChoice(label=label, value=v["code"]))
    return VoiceConnectResponse(voices=choices, message=f"Đã tải {len(choices)} voice AusyncLab.")


@router.post("/ausynclab/test")
def ausynclab_test(body: AusynclabTestRequest):
    from latentsync.tts.ausynclab import AusynclabTTS
    if not body.text.strip():
        raise HTTPException(422, "Nhập câu cần đọc thử.")
    out = str(job_files.UPLOADS_DIR / "ausynclab_test.wav")
    try:
        AusynclabTTS(api_key=body.api_key, default_voice=body.voice or None).synthesize(
            body.text, out, body.voice or None)
    except Exception as e:
        raise HTTPException(502, tts_errors.describe(e, "ausynclab")) from None
    return FileResponse(out, media_type="audio/wav")


@router.post("/autovoice/connect", response_model=VoiceConnectResponse)
def autovoice_connect(body: AutovoiceConnectRequest):
    from latentsync.tts.autovoice import AutoVoiceTTS
    try:
        voices = AutoVoiceTTS(api_key=body.api_key, url=body.url or None,
                              voices_url=body.voices_url or None).fetch_voices()
    except Exception as e:
        raise HTTPException(502, tts_errors.describe(e, "autovoice")) from None
    choices = []
    for v in voices:
        if not v.get("code"):
            continue
        meta = " · ".join(x for x in (v.get("language"), v.get("gender")) if x)
        label = f"{v.get('name') or 'voice'}" + (f" ({meta})" if meta else "") + f" · #{v['code']}"
        choices.append(VoiceChoice(label=label, value=v["code"]))
    return VoiceConnectResponse(voices=choices, message=f"Đã tải {len(choices)} voice hệ thống.")


@router.post("/autovoice/test")
def autovoice_test(body: AutovoiceTestRequest):
    from latentsync.tts.autovoice import AutoVoiceTTS
    if not body.text.strip():
        raise HTTPException(422, "Nhập câu cần đọc thử.")
    out = str(job_files.UPLOADS_DIR / "autovoice_test.wav")
    try:
        AutoVoiceTTS(api_key=body.api_key, default_voice=body.voice or None,
                     url=body.url or None, speed=(body.speed or None)).synthesize(
            body.text, out, body.voice or None)
    except Exception as e:
        raise HTTPException(502, tts_errors.describe(e, "autovoice")) from None
    return FileResponse(out, media_type="audio/wav")
