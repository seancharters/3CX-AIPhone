"""Optional recording of the caller's audio, exactly as the speech-to-text receives it.

For diagnosing transcription accuracy. Switched on in the admin UI; keeps the last few calls.
"""

from __future__ import annotations

import asyncio
import re
import wave
from datetime import datetime
from pathlib import Path

from loguru import logger
from pipecat.frames.frames import Frame, InputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from .settings import DATA_DIR

RECORDINGS_DIR = DATA_DIR / "recordings"
KEEP = 10


class CallerAudioRecorder(FrameProcessor):
    def __init__(self, caller: str) -> None:
        super().__init__()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safe = re.sub(r"[^0-9A-Za-z]+", "_", caller).strip("_") or "unknown"
        self._path = RECORDINGS_DIR / f"{stamp}-{safe}.wav"
        self._audio = bytearray()
        self._sample_rate = 16000

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            self._sample_rate = frame.sample_rate
            self._audio.extend(frame.audio)
        await self.push_frame(frame, direction)

    async def save(self) -> None:
        if not self._audio:
            return
        await asyncio.to_thread(self._write)
        logger.info(f"Saved caller audio to {self._path}")

    def _write(self) -> None:
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        with wave.open(str(self._path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self._sample_rate)
            w.writeframes(bytes(self._audio))
        for old in sorted(RECORDINGS_DIR.glob("*.wav"))[:-KEEP]:
            old.unlink(missing_ok=True)
