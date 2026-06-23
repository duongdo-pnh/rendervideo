"""Audiosynclab (AusyncLab) TTS provider.

Confirmed from the official API-keys doc:
  - Host:   https://api.ausynclab.io/api/v1
  - Auth:   header  X-API-Key: ak_xxxxxxxx   (Master or Sub key; both work on service endpoints)
  - Envelope: { "status": ..., "result": { ... }, "message": ... }   (payload wrapped in "result")
  - Errors:  401 invalid_api_key, 403 api_key_not_master / plan, 409 limit

The TTS *synthesis* endpoint schema was NOT in that doc, so this follows AusyncLab's standard
async pattern (create -> poll state -> download audio_url) and keeps EVERY path/field overridable
via env, plus defensive "dig" for url/id/state so a slightly different schema still works:

  1) POST {base}/speech/text-to-speech  {text, voice_id, speed, language, callback_url} -> result{audio_id|audio_url}
  2) GET  {base}/speech/{audio_id}       poll until state in SUCCEED/SUCCESS/COMPLETED/DONE -> audio_url
  3) GET  audio_url  (follow redirects) -> bytes -> save (transcode to .wav if needed)

When you have the TTS endpoint doc, override via .env (AUSYNCLAB_API_URL, AUSYNCLAB_TTS_PATH, ...).
"""
import os
import subprocess
import tempfile
import time

import requests

from .base import TTSProvider

# Candidate JSON keys (schema varies) — dig recursively through the "result" envelope.
_AUDIO_URL_KEYS = ("audio_url", "audioLink", "audio_link", "url", "link", "public_url", "download_url")
_AUDIO_ID_KEYS = ("audio_id", "id", "request_id", "requestId")
_DONE_STATES = ("SUCCEED", "SUCCESS", "COMPLETED", "DONE", "COMPLETE")
_FAIL_STATES = ("FAILED", "ERROR", "FAIL")


def _dig(obj, keys):
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and obj[k]:
                return obj[k]
        for v in obj.values():
            found = _dig(v, keys)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _dig(v, keys)
            if found:
                return found
    return None


class AusynclabError(RuntimeError):
    pass


class AusynclabTTS(TTSProvider):
    name = "ausynclab"
    label = "Audiosynclab"

    def __init__(self, api_key=None, default_voice=None, base_url=None,
                 language=None, speed=None, timeout=None):
        self.api_key = api_key if api_key is not None else os.getenv("AUSYNCLAB_API_KEY", "")
        self.default_voice = default_voice or os.getenv("AUSYNCLAB_DEFAULT_VOICE", "")
        self.base = (base_url or os.getenv("AUSYNCLAB_API_URL", "https://api.ausynclab.io/api/v1")).rstrip("/")
        self.tts_path = os.getenv("AUSYNCLAB_TTS_PATH", "/speech/text-to-speech")
        self.get_path = os.getenv("AUSYNCLAB_GET_PATH", "/speech/{id}")
        self.voices_path = os.getenv("AUSYNCLAB_VOICES_PATH", "/voices/list")   # đúng theo docs
        self.language = language or os.getenv("AUSYNCLAB_LANGUAGE", "vi")
        self.model = os.getenv("AUSYNCLAB_MODEL", "")                            # myna-1 / myna-1-turbo / myna-2
        # callback_url BẮT BUỘC theo doc (ta poll, không dùng webhook) -> placeholder.
        self.callback_url = os.getenv("AUSYNCLAB_CALLBACK_URL", "https://example.com/ausynclab-callback")
        self.speed = float(speed if speed is not None else os.getenv("AUSYNCLAB_SPEED", "1.0") or 1.0)
        self.output_format = os.getenv("AUSYNCLAB_OUTPUT_FORMAT", "wav")         # audio_url trả WAV trực tiếp
        self.timeout = float(timeout if timeout is not None else os.getenv("AUSYNCLAB_TIMEOUT", "60"))
        self.poll_interval = float(os.getenv("AUSYNCLAB_POLL_INTERVAL", "1.0"))
        self.req_timeout = float(os.getenv("AUSYNCLAB_REQUEST_TIMEOUT", "60"))   # per-HTTP-call timeout

    def _headers(self, json_body=False):
        if not self.api_key:
            raise AusynclabError("AusyncLab cần X-API-Key (.env / tab Cấu hình TTS).")
        h = {"X-API-Key": self.api_key, "accept": "application/json"}
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    @staticmethod
    def _result(data):
        if isinstance(data, dict):
            return data.get("result") or data.get("data") or data
        return data

    # ---- synth ------------------------------------------------------------

    def synthesize(self, text, output_path, voice=None):
        voice = voice or self.default_voice
        self._ensure_parent(output_path)

        resp = self._create_job(text, voice)

        # A) endpoint trả thẳng audio bytes.
        ctype = resp.headers.get("Content-Type", "").lower()
        if ctype.startswith("audio/") or "octet-stream" in ctype:
            return self._save_audio(resp.content, output_path)

        res = self._result(resp.json())
        # B) đã có sẵn audio_url -> tải luôn.
        url = _dig(res, _AUDIO_URL_KEYS)
        # C) chỉ có audio_id -> poll tới khi xong.
        if not url:
            audio_id = _dig(res, _AUDIO_ID_KEYS)
            if not audio_id:
                raise AusynclabError(f"AusyncLab không trả audio_url/audio_id (response: {str(res)[:300]})")
            url = self._poll(audio_id)

        audio = requests.get(url, timeout=120, allow_redirects=True)
        audio.raise_for_status()
        return self._save_audio(audio.content, output_path)

    @staticmethod
    def _audio_name(text):
        name = "".join(c if (c.isalnum() or c in " -_") else " " for c in (text or "")).strip()
        return (name[:60] or "tts")

    @staticmethod
    def _coerce_voice(voice):
        # voice_id của AusyncLab là số nguyên. Chấp nhận: "1928729", hoặc nhãn "... · #1928729".
        import re
        s = str(voice).strip().strip("​‌‍﻿\xa0").strip()   # bỏ cả ký tự ẩn
        if s.isdigit():
            return int(s)
        m = re.search(r"#(\d+)", s)        # rút id từ nhãn hiển thị nếu lỡ lưu cả nhãn
        if m:
            return int(m.group(1))
        return voice

    def _create_job(self, text, voice):
        # audio_name + callback_url là BẮT BUỘC (doc) -> luôn gửi.
        body = {
            "audio_name": self._audio_name(text),
            "text": text,
            "voice_id": self._coerce_voice(voice),
            "callback_url": self.callback_url,
            "speed": self.speed,
            "language": self.language,
        }
        if self.model:
            body["model_name"] = self.model
        r = requests.post(self.base + self.tts_path, json=body,
                          headers=self._headers(json_body=True), timeout=self.req_timeout)
        if r.status_code == 401:
            raise AusynclabError("X-API-Key không hợp lệ (401 invalid_api_key).")
        if not r.ok:                       # lộ message + voice_id đã gửi
            raise AusynclabError(f"AusyncLab {r.status_code} (voice_id={body.get('voice_id')!r}): {r.text[:300]}")
        return r

    def _poll(self, audio_id):
        url = self.base + self.get_path.replace("{id}", str(audio_id))
        end = time.time() + self.timeout
        while time.time() < end:
            r = requests.get(url, headers=self._headers(), timeout=self.req_timeout)
            if r.status_code == 401:
                raise AusynclabError("X-API-Key không hợp lệ (401).")
            r.raise_for_status()
            res = self._result(r.json())
            state = str(res.get("state") or res.get("status") or "").upper()
            link = _dig(res, _AUDIO_URL_KEYS)
            if state in _DONE_STATES and link:
                return link
            if link and not state:        # vài API trả url ngay khi xong, không có state
                return link
            if state in _FAIL_STATES:
                raise AusynclabError(f"AusyncLab TTS thất bại: {str(res)[:200]}")
            time.sleep(self.poll_interval)
        raise AusynclabError("AusyncLab TTS quá thời gian chờ.")

    def _save_audio(self, content, output_path):
        want_wav = output_path.lower().endswith(".wav")
        if want_wav and self.output_format.lower() != "wav":
            with tempfile.NamedTemporaryFile(suffix=f".{self.output_format}", delete=False) as tf:
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

    # ---- voices -----------------------------------------------------------

    # Giọng cố định để hiện sẵn trong dropdown (operator khỏi gõ id). (nhãn, voice_id).
    CURATED_VOICES = [
        ("Nữ · Khả Hân", "508363"),
        ("Nữ · Hamsa test", "1918847"),
        ("Nam · HN - Minh Khang", "319657"),
        ("Nam · An Khôi", "213055"),
    ]

    def voices(self):
        # Chỉ hiện ĐÚNG 4 giọng cố định (không gọi mạng, không liệt kê giọng khác).
        return list(self.CURATED_VOICES)

    def fetch_voices(self):
        """GET {base}/voices -> list dict {code/voice_id, name, ...}. Raise nếu key sai."""
        r = requests.get(self.base + self.voices_path, headers=self._headers(), timeout=self.req_timeout)
        if r.status_code == 401:
            raise AusynclabError("X-API-Key không hợp lệ (401).")
        r.raise_for_status()
        res = self._result(r.json())
        items = res if isinstance(res, list) else (res.get("voices") or res.get("items") or res.get("data") or [])
        out, seen = [], set()
        for v in items:
            if not isinstance(v, dict):
                continue
            code = v.get("id") or v.get("voice_id") or v.get("code")
            if code is not None and code not in seen:
                seen.add(code)
                out.append({"code": str(code), "name": v.get("name"),
                            "gender": v.get("gender"),
                            "language_code": v.get("language") or v.get("language_code"),
                            "use_case": v.get("use_case")})
        return out
