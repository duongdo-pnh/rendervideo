"""Multi-provider TTS layer for the LatentSync render queue.

Public surface:
    from latentsync.tts import synthesize, get_provider, available_providers, list_voices
"""
from .factory import (  # noqa: F401
    synthesize,
    get_provider,
    available_providers,
    list_voices,
    DEFAULT_PROVIDER,
)
from .base import TTSProvider  # noqa: F401
