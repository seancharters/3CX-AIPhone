"""Asterisk integration: the media WebSocket serializer and a minimal AMI client.

Asterisk's chan_websocket connects to the agent for each call and exchanges:
  - BINARY frames: raw audio in the codec chosen in the dialplan (slin16 here)
  - TEXT frames: JSON control events/commands (f(json) in the dialplan)
Docs: https://docs.asterisk.org/Configuration/Channel-Drivers/WebSocket/
"""

from __future__ import annotations

import asyncio
import json

from loguru import logger
from pipecat.audio.dtmf.types import KeypadEntry
from pipecat.audio.utils import create_stream_resampler
from pipecat.frames.frames import (
    AudioRawFrame,
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InputDTMFFrame,
    InterruptionFrame,
)
from pipecat.processors.frame_processor import FrameProcessorSetup
from pipecat.serializers.base_serializer import FrameSerializer

ASTERISK_SAMPLE_RATE = 16000  # slin16
# 20ms of 16-bit mono audio at 16kHz. Asterisk reframes cleanly on multiples of this.
ASTERISK_FRAME_BYTES = ASTERISK_SAMPLE_RATE // 50 * 2


class AsteriskFrameSerializer(FrameSerializer):
    """Converts between Pipecat frames and Asterisk's media WebSocket protocol."""

    def __init__(self) -> None:
        super().__init__(FrameSerializer.InputParams(resampler_clear_after_secs=None))
        self._sample_rate = 0
        self._in_resampler = create_stream_resampler(clear_after_secs=None)
        self._out_resampler = create_stream_resampler(clear_after_secs=None)

    async def setup(self, setup: FrameProcessorSetup) -> None:
        self._sample_rate = setup.audio_in_sample_rate

    async def serialize(self, frame: Frame) -> str | bytes | None:
        if isinstance(frame, InterruptionFrame):
            # Caller spoke over the agent: drop whatever audio Asterisk has queued.
            return json.dumps({"command": "FLUSH_MEDIA"})
        if isinstance(frame, (EndFrame, CancelFrame)):
            return json.dumps({"command": "HANGUP"})
        if isinstance(frame, AudioRawFrame):
            audio = await self._out_resampler.resample(
                frame.audio, frame.sample_rate, ASTERISK_SAMPLE_RATE
            )
            return audio or None
        return None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        if isinstance(data, bytes):
            audio = await self._in_resampler.resample(data, ASTERISK_SAMPLE_RATE, self._sample_rate)
            if not audio:
                return None
            return InputAudioRawFrame(audio=audio, num_channels=1, sample_rate=self._sample_rate)

        try:
            message = json.loads(data)
        except json.JSONDecodeError:
            logger.warning(f"Unexpected control message from Asterisk: {data!r}")
            return None

        event = message.get("event")
        if event == "DTMF_END":
            try:
                return InputDTMFFrame(KeypadEntry(message.get("digit", "")))
            except ValueError:
                return None
        if event == "MEDIA_START":
            logger.info(
                f"Media started: {message.get('channel')} format={message.get('format')} "
                f"frame={message.get('optimal_frame_size')}"
            )
        elif event in ("MEDIA_XOFF", "MEDIA_XON"):
            logger.warning(f"Asterisk flow control: {event}")
        return None


class AMIClient:
    """Just enough of the Asterisk Manager Interface to redirect calls and run CLI commands."""

    def __init__(self, host: str, port: int, username: str, secret: str) -> None:
        self._host, self._port = host, port
        self._username, self._secret = username, secret

    async def redirect(self, channel: str, context: str, exten: str) -> None:
        """Move `channel` to `context`/`exten`, priority 1. Raises on failure."""
        await self._run(Action="Redirect", Channel=channel, Context=context, Exten=exten, Priority="1")

    async def setvar(self, channel: str, variable: str, value: str) -> None:
        """Set a variable on `channel` (it stays set after a redirect). Raises on failure."""
        await self._run(Action="Setvar", Channel=channel, Variable=variable, Value=value)

    async def command(self, command: str) -> str:
        """Run an Asterisk CLI command and return its output."""
        response = await self._run(Action="Command", Command=command)
        return "\n".join(v for k, v in response if k == "Output")

    async def _run(self, **fields: str) -> list[tuple[str, str]]:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port), timeout=5
        )
        try:
            await reader.readline()  # "Asterisk Call Manager/x.y" banner
            await self._action(reader, writer, Action="Login", Username=self._username, Secret=self._secret)
            response = await self._action(reader, writer, **fields)
            writer.write(b"Action: Logoff\r\n\r\n")
            await writer.drain()
            return response
        finally:
            writer.close()

    async def _action(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, **fields: str
    ) -> list[tuple[str, str]]:
        writer.write("".join(f"{k}: {v}\r\n" for k, v in fields.items()).encode() + b"\r\n")
        await writer.drain()
        # Skip any unsolicited events until the response to this action arrives.
        while True:
            message = await asyncio.wait_for(self._read_message(reader), timeout=5)
            response = dict(message).get("Response")
            if response is None:
                continue
            if response not in ("Success", "Follows"):
                raise RuntimeError(f"AMI {fields['Action']} failed: {dict(message).get('Message', message)}")
            return message

    @staticmethod
    async def _read_message(reader: asyncio.StreamReader) -> list[tuple[str, str]]:
        message: list[tuple[str, str]] = []
        while True:
            raw = await reader.readline()
            if not raw:
                raise ConnectionError("AMI connection closed")
            line = raw.decode().rstrip("\r\n")
            if not line:
                return message
            key, _, value = line.partition(":")
            message.append((key.strip(), value.strip()))
