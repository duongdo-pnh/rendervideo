"""Vbee AI Voice provider — implemented per the official Vbee API docs.

Auth (every request): two headers
    App-Id:        <app_id>
    Authorization: Bearer <access_token>

TTS is ASYNC only (the common user package rejects mode:sync):
    1) POST  https://api.vbee.vn/v1/tts        {text, voiceCode, mode:"async", speed,
                                                 outputFormat:"mp3", webhookUrl}  -> {requestId, status}
    2) GET   https://api.vbee.vn/v1/tts/requests/{requestId}   poll until status COMPLETED -> audioLink
    3) GET   audioLink  (302 -> CDN, follow redirects) -> mp3 bytes -> save (transcode to .wav if needed)

Voices: GET https://vbee.vn/api/public/v1/voices?voiceOwnership=VBEE&limit<=100  (cursor pagination).

Docs: https://api-docs.vbee.vn/
"""
import os
import subprocess
import tempfile
import time

import requests

from .base import TTSProvider

TTS_URL = "https://api.vbee.vn/v1/tts"
STATUS_URL = "https://api.vbee.vn/v1/tts/requests"          # + /{requestId}
VOICES_URL = "https://vbee.vn/api/public/v1/voices"
OWNERSHIPS = ("VBEE", "PERSONAL", "COMMUNITY")


class VbeeError(RuntimeError):
    pass


def _clean_voice(v):
    """Bỏ khoảng trắng + ký tự ẩn (zero-width, BOM, nbsp) thường dính khi gõ/paste trong Excel."""
    if not v:
        return v
    return str(v).strip().strip("​‌‍﻿\xa0").strip()


class VbeeTTS(TTSProvider):
    name = "vbee"
    label = "Vbee"

    def __init__(self, app_id=None, token=None, default_voice=None,
                 webhook_url=None, speed=None, timeout=None):
        # Explicit args override env (so the UI can test unsaved credentials).
        self.app_id = app_id if app_id is not None else os.getenv("VBEE_APP_ID", "")
        self.token = token if token is not None else os.getenv("VBEE_TOKEN", "")
        self.default_voice = default_voice or os.getenv(
            "VBEE_DEFAULT_VOICE", "hn_female_ngochuyen_full_48k-fhg")
        # webhookUrl BẮT BUỘC & phải khác rỗng. Nếu .env lưu rỗng -> rơi về placeholder.
        self.webhook_url = (webhook_url or (os.getenv("VBEE_WEBHOOK_URL") or "").strip()
                            or "https://example.com/vbee-callback")
        self.speed = float(speed if speed is not None else os.getenv("VBEE_SPEED", "1.0") or 1.0)
        self.output_format = os.getenv("VBEE_OUTPUT_FORMAT", "mp3")
        self.timeout = float(timeout if timeout is not None else os.getenv("VBEE_TIMEOUT", "60"))
        self.poll_interval = float(os.getenv("VBEE_POLL_INTERVAL", "1.0"))
        self.tts_url = os.getenv("VBEE_API_URL", TTS_URL)
        self.status_url = os.getenv("VBEE_STATUS_URL", STATUS_URL)
        self.voices_url = os.getenv("VBEE_VOICES_URL", VOICES_URL)

    # ---- auth -------------------------------------------------------------

    def _headers(self, json_body=False):
        if not self.app_id or not self.token:
            raise VbeeError("Vbee cần cả App ID và Access Token (.env / tab Cấu hình TTS).")
        h = {"App-Id": self.app_id, "Authorization": f"Bearer {self.token}"}
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    @staticmethod
    def _result(data):
        """Vbee bọc payload trong 'result' (hoặc 'data'); trả về phần lõi."""
        if isinstance(data, dict):
            return data.get("result") or data.get("data") or data
        return data

    # ---- synth ------------------------------------------------------------

    def synthesize(self, text, output_path, voice=None):
        voice = _clean_voice(voice or self.default_voice)
        self._ensure_parent(output_path)
        req_id = self._create_job(text, voice)
        audio_link = self._poll(req_id)
        audio = requests.get(audio_link, timeout=120, allow_redirects=True)  # 302 -> CDN
        audio.raise_for_status()
        self._save_audio(audio.content, output_path)
        return output_path

    def _create_job(self, text, voice):
        body = {
            "text": text,
            "voiceCode": voice,
            "mode": "async",                 # gói phổ thông không hỗ trợ sync
            "speed": self.speed,
            "outputFormat": self.output_format,
            "webhookUrl": self.webhook_url,  # bắt buộc dù ta poll, không dùng webhook
        }
        r = requests.post(self.tts_url, json=body, headers=self._headers(json_body=True), timeout=30)
        if r.status_code == 401:
            raise VbeeError("Sai App ID hoặc Token (401).")
        if not r.ok:                       # lộ message + voiceCode đã gửi (để thấy ký tự lạ nếu có)
            raise VbeeError(f"Vbee {r.status_code} (voiceCode={voice!r}): {r.text[:300]}")
        res = self._result(r.json())
        req_id = res.get("requestId") or res.get("request_id")
        if not req_id:
            raise VbeeError(f"Vbee không trả requestId (response: {str(res)[:300]})")
        return req_id

    def _poll(self, req_id):
        end = time.time() + self.timeout
        while time.time() < end:
            r = requests.get(f"{self.status_url}/{req_id}", headers=self._headers(), timeout=30)
            if r.status_code == 401:
                raise VbeeError("Sai App ID hoặc Token (401).")
            r.raise_for_status()
            res = self._result(r.json())
            status = str(res.get("status", "")).upper()
            if status == "COMPLETED":
                link = res.get("audioLink") or res.get("audio_link")
                if link:
                    return link
                raise VbeeError(f"Vbee COMPLETED nhưng thiếu audioLink: {str(res)[:200]}")
            if status == "FAILED":
                raise VbeeError(f"Vbee TTS FAILED: {str(res)[:200]}")
            time.sleep(self.poll_interval)
        raise VbeeError("Vbee TTS quá thời gian chờ.")

    def _save_audio(self, content, output_path):
        """Lưu audio. Nếu cần .wav mà Vbee trả mp3 -> transcode bằng ffmpeg (sạch cho downstream)."""
        want_wav = output_path.lower().endswith(".wav")
        if want_wav and self.output_format.lower() != "wav":
            with tempfile.NamedTemporaryFile(suffix=f".{self.output_format}", delete=False) as tf:
                tf.write(content)
                tmp = tf.name
            try:
                subprocess.run(["ffmpeg", "-y", "-i", tmp, output_path],
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                # ffmpeg thiếu/ lỗi -> ghi thẳng bytes (worker normalize_audio vẫn đọc được qua ffmpeg).
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

    # ---- voices -----------------------------------------------------------

    # Giọng quảng cáo clone (_advertise_vc): gọi TTS được nhưng KHÔNG xuất hiện trong /voices,
    # nên liệt kê cứng để hiện trong dropdown (operator khỏi gõ tay code). (nhãn, voiceCode).
    CURATED_VOICES = [
        ("Nữ · Quảng cáo 02 (Bắc)", "n_hanoi_female_quangcao02_advertise_vc"),
        ("Nữ · Vy Quảng Cáo (Nam)", "s_hochiminh_female_vyquangcao_advertise_vc"),
        ("Nam · Thắng chuyên nghiệp (Bắc)", "n_hanoi_male_thangchuyennghiep_advertise_vc"),
        ("Nam · Quang Quảng Cáo (Bắc)", "n_hanoi_male_quangquangcao_advertise_vc"),
    ]

    def voices(self):
        # Chỉ hiện ĐÚNG 4 giọng cố định (không gọi mạng, không liệt kê giọng khác).
        return list(self.CURATED_VOICES)

    def fetch_voices(self, limit=100):
        """Gọi Vbee lấy TẤT CẢ giọng (VBEE + PERSONAL + COMMUNITY), gộp & khử trùng.

        Trả về list dict {code, name, gender, language_code, demo}. Raise nếu auth sai.
        """
        limit = min(int(limit), 100)
        seen, out = set(), []
        for own in OWNERSHIPS:
            cursor = None
            for _ in range(50):  # trần phân trang an toàn
                params = {"voiceOwnership": own, "limit": limit}
                if cursor:
                    params["cursor"] = cursor
                r = requests.get(self.voices_url, params=params, headers=self._headers(), timeout=30)
                if r.status_code == 401:
                    raise VbeeError("Sai App ID hoặc Token (401).")
                r.raise_for_status()
                res = self._result(r.json())
                items = (res.get("voices") or res.get("items") or res.get("data")
                         or (res if isinstance(res, list) else [])) or []
                for v in items:
                    code = (v or {}).get("code")
                    if code and code not in seen:
                        seen.add(code)
                        out.append({k: v.get(k) for k in
                                    ("code", "name", "gender", "language_code", "demo")})
                has_next = bool(res.get("has_next_page") or res.get("hasNextPage"))
                cursor = res.get("next_cursor") or res.get("nextCursor")
                if not has_next or not cursor:
                    break
        return out
