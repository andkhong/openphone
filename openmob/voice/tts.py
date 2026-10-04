"""Spoken replies through macOS `say`."""

from __future__ import annotations

import shutil
import subprocess
import sys


class Speaker:
    def __init__(self, enabled: bool = True, voice: str | None = None):
        self.enabled = enabled and sys.platform == "darwin" and shutil.which("say") is not None
        self.voice = voice
        self._proc: subprocess.Popen | None = None

    def say(self, text: str, block: bool = False) -> None:
        if not self.enabled or not text:
            return
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()  # newer speech replaces older speech
        cmd = ["say"] + (["-v", self.voice] if self.voice else []) + [text]
        self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if block:
            self._proc.wait()

    def chime(self) -> None:
        if self.enabled:
            subprocess.Popen(["afplay", "/System/Library/Sounds/Tink.aiff"], stderr=subprocess.DEVNULL)
