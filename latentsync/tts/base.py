"""TTSProvider base class — common contract for every TTS backend.

A provider turns `text` into a WAV file at `output_path` and returns that path.
Subclasses read their own config from environment variables in __init__ (so the
factory can construct them with no arguments) and override synthesize().
"""
from pathlib import Path


class TTSProvider:
    # Human label + short note shown by the UI / GET providers list. Override per subclass.
    name = "base"
    label = "Base"

    def synthesize(self, text: str, output_path: str, voice: str = None) -> str:
        """Synthesize `text` to a WAV at `output_path`; return output_path.

        Implementations MUST raise on failure (so excel_import can mark that row
        failed without aborting the whole batch) rather than writing a bad file.
        """
        raise NotImplementedError

    # ---- helpers shared by the online providers ----------------------------

    @staticmethod
    def _ensure_parent(output_path: str) -> None:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    def voices(self):
        """Optional: list of known voice codes for this provider (for the UI dropdown).

        Default: just the configured default voice, if any. Override to enumerate more.
        """
        dv = getattr(self, "default_voice", None)
        return [dv] if dv else []
