"""Local (offline) TTS provider — Piper or Coqui.

Skeleton per the plan: runs a TTS model on this machine. Imports are lazy so the
factory never breaks if piper/TTS isn't installed. The model is loaded once and reused.
"""
import os
import wave

from .base import TTSProvider


class LocalTTS(TTSProvider):
    name = "local"
    label = "Local"

    def __init__(self):
        self.engine = os.getenv("LOCAL_TTS_ENGINE", "piper").lower()
        self.model_path = os.getenv("LOCAL_TTS_MODEL_PATH", "models/tts/vi_VN")
        self.default_voice = os.getenv("LOCAL_TTS_DEFAULT_VOICE", "vi_VN-thuy-medium")
        self._model = None

    def _load_model(self):
        if self._model is not None:
            return
        if self.engine == "piper":
            try:
                from piper import PiperVoice
            except ImportError as e:
                raise RuntimeError("Provider 'local' (piper) cần: pip install piper-tts") from e
            self._model = PiperVoice.load(self.model_path)
        elif self.engine == "coqui":
            try:
                from TTS.api import TTS
            except ImportError as e:
                raise RuntimeError("Provider 'local' (coqui) cần: pip install TTS") from e
            self._model = TTS(self.model_path)
        else:
            raise ValueError(f"Unknown LOCAL_TTS_ENGINE: {self.engine}")

    def synthesize(self, text, output_path, voice=None):
        self._load_model()
        self._ensure_parent(output_path)
        voice = voice or self.default_voice

        if self.engine == "piper":
            with wave.open(output_path, "wb") as wav:
                self._model.synthesize(text, wav)
        elif self.engine == "coqui":
            self._model.tts_to_file(text=text, file_path=output_path)
        return output_path
