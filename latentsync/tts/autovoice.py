"""Voice he thong (voice.autovideo.vn) TTS provider.

API shape, ported from lives.git:
  POST {AUTOVOICE_URL} with X-API-Key and JSON
  {"text": "...", "voice_name": "...", "speed": 1.2}

The response can be audio bytes, JSON with an audio URL, or JSON with base64 audio.
This provider always writes to the requested output_path, transcoding to WAV when needed.
"""
import base64
import os
import subprocess
import tempfile

import requests

from .base import TTSProvider

DEFAULT_URL = "https://voice.autovideo.vn/v1/tts"
MAX_CHARS = 2000


def _float_value(value, default):
    value = default if value is None or str(value).strip() == "" else value
    return float(value)


class AutoVoiceError(RuntimeError):
    def __init__(self, message, status_code=None, retry_after=None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


def _dig_audio_url(obj):
    if isinstance(obj, str):
        s = obj.strip()
        if s.startswith("http") and (".mp3" in s or ".wav" in s or "audio" in s.lower()):
            return s
        return None
    if isinstance(obj, dict):
        for k in ("audio_url", "audioUrl", "audio_link", "audioLink", "url", "link", "file_url", "output_url"):
            v = obj.get(k)
            if isinstance(v, str) and v.startswith("http"):
                return v
        for v in obj.values():
            u = _dig_audio_url(v)
            if u:
                return u
    if isinstance(obj, list):
        for v in obj:
            u = _dig_audio_url(v)
            if u:
                return u
    return None


def _dig_base64(obj):
    if isinstance(obj, dict):
        for k in ("audio_base64", "audioBase64", "audioContent", "audio_content", "audio", "data", "content"):
            v = obj.get(k)
            if isinstance(v, str) and len(v) > 200 and not v.startswith("http"):
                return v
        for v in obj.values():
            b = _dig_base64(v)
            if b:
                return b
    return None


def _looks_like_audio(content, ctype):
    ctype = (ctype or "").lower()
    if "audio" in ctype or "octet-stream" in ctype or "mpeg" in ctype:
        return True
    if not content:
        return False
    head = content[:4]
    return head[:3] == b"ID3" or head[:2] in (b"\xff\xfb", b"\xff\xf3") or head in (b"RIFF", b"OggS")


def _ext_of(content):
    head = content[:4] if content else b""
    if head == b"RIFF":
        return "wav"
    if head == b"OggS":
        return "ogg"
    return "mp3"


class AutoVoiceTTS(TTSProvider):
    name = "autovoice"
    label = "Voice he thong"

    def __init__(self, api_key=None, default_voice=None, url=None, voices_url=None,
                 speed=None, timeout=None):
        self.api_key = api_key if api_key is not None else os.getenv("AUTOVOICE_API_KEY", "")
        self.default_voice = default_voice or os.getenv("AUTOVOICE_DEFAULT_VOICE", "")
        self.url = (url or os.getenv("AUTOVOICE_URL", DEFAULT_URL)).strip() or DEFAULT_URL
        self.voices_url = (voices_url or os.getenv("AUTOVOICE_VOICES_URL", "")).strip()
        self.speed = _float_value(speed if speed is not None else (
            os.getenv("AUTOVOICE_SPEED") or os.getenv("TTS_SPEED")
        ), 1.2)
        self.timeout = _float_value(timeout if timeout is not None else os.getenv("AUTOVOICE_TIMEOUT"), 60)

    def _headers(self):
        if not self.api_key:
            raise AutoVoiceError("Voice he thong can AUTOVOICE_API_KEY (.env / tab Cau hinh TTS).")
        return {"X-API-Key": self.api_key, "Content-Type": "application/json"}

    def _voices_url(self):
        if self.voices_url:
            return self.voices_url
        if self.url.endswith("/tts"):
            return self.url[:-4] + "/voices"
        return self.url.rsplit("/", 1)[0] + "/voices"

    def synthesize(self, text, output_path, voice=None):
        voice = (voice or self.default_voice or "").strip()
        if not voice:
            raise AutoVoiceError("Chua nhap ma giong AUTOVOICE_DEFAULT_VOICE / voice_name.")
        text = (text or "").strip()
        if not text:
            raise AutoVoiceError("Text rong.")
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS].rsplit(" ", 1)[0] or text[:MAX_CHARS]

        self._ensure_parent(output_path)
        payload = {"text": text, "voice_name": voice, "speed": self.speed}
        try:
            r = requests.post(self.url, headers=self._headers(), json=payload, timeout=self.timeout)
        except Exception as e:
            raise AutoVoiceError(f"Khong goi duoc Voice he thong: {e}") from e

        if r.status_code in (401, 403):
            raise AutoVoiceError(f"Sai AUTOVOICE_API_KEY ({r.status_code}).", status_code=r.status_code)
        if r.status_code >= 400:
            raise AutoVoiceError(f"Voice he thong HTTP {r.status_code}: {r.text[:200]}",
                                 status_code=r.status_code, retry_after=r.headers.get("Retry-After"))

        ctype = r.headers.get("Content-Type", "")
        if _looks_like_audio(r.content, ctype):
            return self._save_audio(r.content, output_path)

        try:
            body = r.json()
        except Exception:
            if r.content and len(r.content) > 500:
                return self._save_audio(r.content, output_path)
            raise AutoVoiceError(f"Response khong doc duoc (content-type={ctype}).")

        url = _dig_audio_url(body)
        if url:
            ar = requests.get(url, timeout=120, allow_redirects=True)
            ar.raise_for_status()
            return self._save_audio(ar.content, output_path)

        b64 = _dig_base64(body)
        if b64:
            try:
                raw = base64.b64decode(b64 + "=" * (-len(b64) % 4))
            except Exception as e:
                raise AutoVoiceError(f"Giai ma base64 loi: {e}") from e
            return self._save_audio(raw, output_path)

        raise AutoVoiceError(f"Khong tim thay audio trong response: {str(body)[:200]}")

    def _save_audio(self, content, output_path):
        want_wav = output_path.lower().endswith(".wav")
        ext = _ext_of(content)
        if want_wav and ext != "wav":
            with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tf:
                tf.write(content)
                tmp = tf.name
            try:
                subprocess.run(["ffmpeg", "-y", "-i", tmp, output_path],
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                with open(output_path, "wb") as f:
                    f.write(content)
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        else:
            with open(output_path, "wb") as f:
                f.write(content)
        return output_path

    def voices(self):
        return [self.default_voice] if self.default_voice else []

    def fetch_voices(self):
        r = requests.get(self._voices_url(), headers={"X-API-Key": self.api_key}, timeout=self.timeout)
        if r.status_code in (401, 403):
            raise AutoVoiceError(f"Sai AUTOVOICE_API_KEY ({r.status_code}).", status_code=r.status_code)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        body = r.json()
        data = body
        if isinstance(body, dict):
            data = (body.get("voices") or body.get("data") or body.get("result")
                    or body.get("items") or body.get("list") or [])
            if isinstance(data, dict):
                data = data.get("voices") or data.get("items") or data.get("list") or []
        out, seen = [], set()
        if isinstance(data, list):
            for v in data:
                if isinstance(v, str):
                    code, name, gender, lang = v, v, "", ""
                elif isinstance(v, dict):
                    code = v.get("voice_name") or v.get("code") or v.get("id") or v.get("name")
                    name = v.get("name") or v.get("title") or v.get("display_name") or code
                    gender = v.get("gender") or ""
                    lang = v.get("language") or v.get("lang") or v.get("language_code") or ""
                else:
                    continue
                if not code or code in seen:
                    continue
                seen.add(code)
                out.append({"code": str(code), "name": str(name or code),
                            "gender": gender, "language": lang})
        return out
