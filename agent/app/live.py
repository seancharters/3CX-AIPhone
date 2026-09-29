"""Live call events for the admin UI's "Live calls" tab.

The agent publishes events here as calls happen; the admin container streams them from the
agent's internal /events endpoint (server-sent events). The last few calls are kept in memory
so the page shows recent calls when opened. Nothing is written to disk.

Event shapes (all include "call_id" and "time"):
  {"type": "call_start", "caller": ..., "number": ..., "info": ...}
  {"type": "line", "line_id": ..., "speaker": "caller" | "agent", "text": ..., "final": bool}
  {"type": "note", "text": ...}           e.g. ticket created, transferring
  {"type": "call_end"}
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections import OrderedDict
from typing import Any

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    TranscriptionFrame,
    TTSTextFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

MAX_CALLS = 10


class LiveBus:
    def __init__(self) -> None:
        # call_id -> events, with each line kept only at its latest version
        self._calls: OrderedDict[str, OrderedDict[str, dict[str, Any]]] = OrderedDict()
        self._subscribers: set[asyncio.Queue] = set()
        self._ids = itertools.count(1)

    def new_id(self) -> str:
        return str(next(self._ids))

    def publish(self, event: dict[str, Any]) -> None:
        event = {"time": time.time(), **event}
        call = self._calls.setdefault(event["call_id"], OrderedDict())
        key = f"line:{event['line_id']}" if event["type"] == "line" else f"ev:{self.new_id()}"
        call[key] = event
        while len(self._calls) > MAX_CALLS:
            self._calls.popitem(last=False)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass  # a stalled viewer misses events rather than slowing calls down

    def snapshot(self) -> list[dict[str, Any]]:
        return [e for call in self._calls.values() for e in call.values()]

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)


bus = LiveBus()


class TranscriptTap(FrameProcessor):
    """Publishes what's said on a call.

    As "caller", sits after speech-to-text. As "agent", sits after the audio output, where
    spoken text passes in step with the audio (so interrupted speech shows only what was heard).
    """

    def __init__(self, call_id: str, speaker: str) -> None:
        super().__init__()
        self._call_id = call_id
        self._speaker = speaker
        self._line_id: str | None = None
        self._text = ""

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if self._speaker == "caller":
                self._caller(frame)
            else:
                self._agent(frame)
        await self.push_frame(frame, direction)

    def _caller(self, frame: Frame) -> None:
        if isinstance(frame, InterimTranscriptionFrame):
            # Some providers send the whole phrase so far, others (whisper) only the new words.
            if frame.text.startswith(self._text):
                self._text = frame.text
            else:
                self._text += frame.text
            self._emit(final=False)
        elif isinstance(frame, TranscriptionFrame):
            self._text = frame.text
            self._emit(final=True)

    def _agent(self, frame: Frame) -> None:
        if isinstance(frame, BotStartedSpeakingFrame):
            self._line_id, self._text = None, ""
        elif isinstance(frame, TTSTextFrame) and frame.text:
            joiner = "" if not self._text or frame.text[:1] in ".,!?;:'’" or self._text.endswith(" ") else " "
            self._text += joiner + frame.text
            self._emit(final=False)
        elif isinstance(frame, (BotStoppedSpeakingFrame, InterruptionFrame)) and self._text:
            if isinstance(frame, InterruptionFrame):
                self._text += " …(interrupted)"
            self._emit(final=True)

    def _emit(self, final: bool) -> None:
        text = self._text.strip()
        if not text:
            return
        if self._line_id is None:
            self._line_id = bus.new_id()
        bus.publish(
            {"type": "line", "call_id": self._call_id, "line_id": self._line_id,
             "speaker": self._speaker, "text": text, "final": final}
        )
        if final:
            self._line_id, self._text = None, ""
