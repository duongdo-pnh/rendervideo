"""Generic online TTS provider — OpenAI TTS / ElevenLabs / (extensible).

Skeleton per the plan: real calls require the corresponding SDK / key. Imports are
lazy so this module never breaks the factory if `openai` isn't installed.
"""
import os

import requests

from .base import TTSProvider


class ApiOnlineTTS(TTSProvider):
    name = "api"
    label = "API Online"

    def __init__(self):
        self.api_key = os.getenv("TTS_API_KEY")
        self.api_url = os.getenv("TTS_API_URL")
        self.api_type = os.getenv("TTS_API_TYPE", "openai").lower()  # openai / elevenlabs
        self.default_voice = os.getenv("TTS_API_DEFAULT_VOICE", "alloy")

    def synthesize(self, text, output_path, voice=None):
        if not self.api_key:
            raise RuntimeError("TTS_API_KEY chưa cấu hình (.env).")
        voice = voice or self.default_voice
        self._ensure_parent(output_path)

        if self.api_type == "openai":
            return self._openai(text, output_path, voice)
        elif self.api_type == "elevenlabs":
            return self._elevenlabs(text, output_path, voice)
        else:
            raise ValueError(f"Unknown api_type: {self.api_type}")

    def _openai(self, text, output_path, voice):
        try:
            from openai import OpenAI
        except ImportError as e:
            raise RuntimeError("Provider 'api' (openai) cần: pip install openai") from e
        client = OpenAI(api_key=self.api_key)
        with client.audio.speech.with_streaming_response.create(
            model=os.getenv("TTS_API_MODEL", "tts-1"),
            voice=voice, input=text, response_format="wav",
        ) as response:
            response.stream_to_file(output_path)
        return output_path

    def _elevenlabs(self, text, output_path, voice):
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice}"
        resp = requests.post(
            url, json={"text": text},
            headers={"xi-api-key": self.api_key}, timeout=60,
        )
        resp.raise_for_status()
        with open(output_path, "wb") as f:
            f.write(resp.content)
        return output_path

    def voices(self):
        if self.api_type == "openai":
            return ["alloy", "echo", "fable", "onyx", "nova", "shimmer"]
        return [self.default_voice] if self.default_voice else []
