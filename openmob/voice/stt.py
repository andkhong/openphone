"""Speech-to-text with Whisper, run on the server for the dashboard's mic button.

Needs the `voice` extra: pip install -e '.[voice]'
"""

from __future__ import annotations

import threading


class Transcriber:
    """Lazily loads a faster-whisper model and transcribes audio files (any format PyAV can decode)."""

    def __init__(self, model_size: str = "base.en"):
        self.model_size = model_size
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(self.model_size, device="auto", compute_type="int8")
        return self._model

    def transcribe(self, audio) -> str:
        """`audio` is a file path or a binary file object (e.g. a browser recording)."""
        with self._lock:
            model = self._load()
            language = "en" if self.model_size.endswith(".en") else None
            segments, _ = model.transcribe(audio, beam_size=1, language=language, vad_filter=True)
            return " ".join(s.text.strip() for s in segments).strip()
