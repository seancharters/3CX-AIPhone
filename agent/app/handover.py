"""The spoken handover for announced transfers.

Before connecting the caller, Asterisk plays the engineer a short recording of the agent saying
who's calling and why. The recording is made here with the same voice as the call and saved to
the shared /data volume, which Asterisk mounts read-only.
"""

from __future__ import annotations

import secrets
import time
from xml.sax.saxutils import escape

import aiohttp

from .settings import DATA_DIR

HANDOVER_DIR = DATA_DIR / "handovers"
KEEP_SECONDS = 3600


async def record(cfg: dict[str, str], text: str) -> str:
    """Synthesise `text` and save it for Asterisk. Returns the file's ID (its name, no extension)."""
    audio = await _synthesise(cfg, text)
    HANDOVER_DIR.mkdir(parents=True, exist_ok=True)
    HANDOVER_DIR.chmod(0o755)
    for old in HANDOVER_DIR.glob("*.sln16"):
        if old.stat().st_mtime < time.time() - KEEP_SECONDS:
            old.unlink(missing_ok=True)
    file_id = secrets.token_hex(8)
    path = HANDOVER_DIR / f"{file_id}.sln16"  # raw 16kHz 16-bit mono, which Asterisk plays as-is
    path.write_bytes(audio)
    path.chmod(0o644)  # Asterisk runs as a different user
    return file_id


async def _synthesise(cfg: dict[str, str], text: str) -> bytes:
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as http:
        if cfg["TTS_PROVIDER"] == "azure":
            voice = cfg["AZURE_TTS_VOICE"]
            ssml = (
                f"<speak version='1.0' xml:lang='en-AU'><voice name='{escape(voice)}'>"
                f"{escape(text)}</voice></speak>"
            )
            response = await http.post(
                f"https://{cfg['AZURE_SPEECH_REGION']}.tts.speech.microsoft.com/cognitiveservices/v1",
                headers={
                    "Ocp-Apim-Subscription-Key": cfg["AZURE_SPEECH_KEY"] or cfg["AZURE_OPENAI_API_KEY"],
                    "Content-Type": "application/ssml+xml",
                    "X-Microsoft-OutputFormat": "raw-16khz-16bit-mono-pcm",
                },
                data=ssml.encode(),
            )
        else:
            voice = cfg["ELEVENLABS_VOICE_ID"] or "IKne3meq5aSn9XLyUdCD"
            response = await http.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
                params={"output_format": "pcm_16000"},
                headers={"xi-api-key": cfg["ELEVEN_API_KEY"]},
                json={"text": text, "model_id": cfg["ELEVENLABS_MODEL"]},
            )
        async with response:
            if response.status != 200:
                raise RuntimeError(f"Voice service returned {response.status}: {(await response.text())[:200]}")
            audio = await response.read()
    if not audio:
        raise RuntimeError("Voice service returned no audio")
    return audio
