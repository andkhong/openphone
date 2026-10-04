"""Speech-to-text with local Whisper, plus push-to-talk on the Mac microphone.

Needs the `voice` extra: pip install -e '.[voice]'
"""

from __future__ import annotations

import threading
from typing import Callable

SAMPLE_RATE = 16_000
MIN_SECONDS = 0.3


class Transcriber:
    """Lazily loads a faster-whisper model and transcribes audio arrays or files."""

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
        """`audio` is a float32 16 kHz numpy array, a file path, or a binary file object."""
        with self._lock:
            model = self._load()
            language = "en" if self.model_size.endswith(".en") else None
            segments, _ = model.transcribe(audio, beam_size=1, language=language, vad_filter=True)
            return " ".join(s.text.strip() for s in segments).strip()


def parse_hotkey(name: str):
    from pynput import keyboard

    return getattr(keyboard.Key, name, None) or keyboard.KeyCode.from_char(name)


class PushToTalk:
    """Hold the hotkey to record; release to transcribe. Esc calls on_cancel."""

    def __init__(
        self,
        transcriber: Transcriber,
        on_text: Callable[[str], None],
        on_cancel: Callable[[], None],
        hotkey: str = "alt_r",
        on_listen: Callable[[], None] | None = None,
    ):
        self.transcriber = transcriber
        self.on_text = on_text
        self.on_cancel = on_cancel
        self.on_listen = on_listen
        self.hotkey = parse_hotkey(hotkey)
        self._chunks: list = []
        self._stream = None
        self._listener = None

    def start(self) -> None:
        import sounddevice  # noqa: F401  (fail early if PortAudio is missing)
        from pynput import keyboard

        self._listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self._listener.start()

    def _on_press(self, key) -> None:
        from pynput import keyboard

        if key == keyboard.Key.esc:
            self.on_cancel()
        elif key == self.hotkey and self._stream is None:
            import sounddevice as sd

            self._chunks = []
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="float32",
                callback=lambda data, *_: self._chunks.append(data.copy()),
            )
            self._stream.start()
            if self.on_listen:
                self.on_listen()

    def _on_release(self, key) -> None:
        if key != self.hotkey or self._stream is None:
            return
        import numpy as np

        self._stream.stop()
        self._stream.close()
        self._stream = None
        if not self._chunks:
            return
        audio = np.concatenate(self._chunks)[:, 0]
        if len(audio) < SAMPLE_RATE * MIN_SECONDS:
            return
        threading.Thread(target=self._finish, args=(audio,), daemon=True).start()

    def _finish(self, audio) -> None:
        text = self.transcriber.transcribe(audio)
        if text:
            self.on_text(text)
