"""Speech-to-text diagnostics on a recorded call (see recorder.py).

    docker compose exec agent python -m app.diagnostics            # latest recording
    docker compose exec agent python -m app.diagnostics FILE.wav

Reports audio quality (level, clipping, whether it's really wideband) and transcribes the
recording with each configured speech-to-text option so they can be compared.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import time
import wave

import numpy as np

from . import settings
from .bot import _azure_host, vocabulary
from .recorder import RECORDINGS_DIR


def load(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return pcm, rate


def speech_segments(pcm: np.ndarray, rate: int, min_gap: float = 0.6) -> list[tuple[int, int]]:
    """Rough utterance boundaries from energy, to feed engines one phrase at a time like a call does."""
    frame = rate // 50  # 20 ms
    rms = np.array([np.sqrt(np.mean(pcm[i : i + frame].astype(np.float64) ** 2)) for i in range(0, len(pcm) - frame, frame)])
    if not len(rms):
        return []
    threshold = max(np.percentile(rms, 30) * 3, 150)
    voiced = rms > threshold
    segments, start, silent = [], None, 0
    for i, v in enumerate(voiced):
        if v:
            start = i if start is None else start
            silent = 0
        elif start is not None:
            silent += 1
            if silent * 0.02 >= min_gap:
                segments.append((start * frame, (i - silent + 1) * frame))
                start, silent = None, 0
    if start is not None:
        segments.append((start * frame, len(pcm)))
    pad = int(0.25 * rate)
    return [(max(0, a - pad), min(len(pcm), b + pad)) for a, b in segments if (b - a) > 0.15 * rate]


def audio_report(pcm: np.ndarray, rate: int, segments: list[tuple[int, int]]) -> None:
    x = pcm.astype(np.float64) / 32768
    speech = np.concatenate([x[a:b] for a, b in segments]) if segments else x
    dbfs = lambda v: 20 * np.log10(max(np.sqrt(np.mean(v**2)), 1e-9))
    print(f"Length: {len(pcm) / rate:.1f}s at {rate} Hz, {len(segments)} speech segments")
    print(f"Speech level: {dbfs(speech):.1f} dBFS (good: -30 to -15)   Peak: {20*np.log10(max(np.abs(x).max(),1e-9)):.1f} dBFS")
    print(f"Clipped samples: {100 * np.mean(np.abs(pcm) >= 32700):.3f}% (should be ~0)")
    mask = np.ones(len(x), bool)
    for a, b in segments:
        mask[a:b] = False
    if mask.any():
        print(f"Background noise (between phrases): {dbfs(x[mask]):.1f} dBFS  -> signal-to-noise ≈ {dbfs(speech) - dbfs(x[mask]):.0f} dB (good: > 25)")
    spec = np.abs(np.fft.rfft(speech[: rate * 30])) ** 2
    freqs = np.fft.rfftfreq(len(speech[: rate * 30]), 1 / rate)
    high = spec[freqs > 4000].sum() / max(spec.sum(), 1e-12)
    print(f"Energy above 4 kHz: {100 * high:.2f}% " + ("(wideband/HD audio: good)" if high > 0.001 else "(looks narrowband: phone-quality audio)"))


def azure_speech(pcm: np.ndarray, rate: int, cfg: dict[str, str], phrases: list[str] | None = None) -> list[str]:
    import azure.cognitiveservices.speech as sdk

    key = cfg["AZURE_SPEECH_KEY"] or cfg["AZURE_OPENAI_API_KEY"]
    conf = sdk.SpeechConfig(subscription=key, region=cfg["AZURE_SPEECH_REGION"])
    conf.speech_recognition_language = "en-AU"
    stream = sdk.audio.PushAudioInputStream(sdk.audio.AudioStreamFormat(samples_per_second=rate, bits_per_sample=16, channels=1))
    rec = sdk.SpeechRecognizer(speech_config=conf, audio_config=sdk.audio.AudioConfig(stream=stream))
    if phrases:
        grammar = sdk.PhraseListGrammar.from_recognizer(rec)
        for p in phrases:
            grammar.addPhrase(p)
    out: list[str] = []
    finished: list[int] = []
    rec.recognized.connect(lambda e: out.append(e.result.text) if e.result.text else None)
    rec.session_stopped.connect(lambda e: finished.append(1))
    rec.canceled.connect(lambda e: (out.append(f"[error: {e.cancellation_details.error_details}]") if e.cancellation_details.error_details else None, finished.append(1)))
    rec.start_continuous_recognition()
    stream.write(pcm.tobytes())
    stream.close()
    t = time.time()
    while not finished and time.time() - t < 120:
        time.sleep(0.2)
    rec.stop_continuous_recognition()
    return out


async def realtime_whisper(pcm: np.ndarray, rate: int, segments, cfg: dict[str, str]) -> list[str]:
    import websockets

    endpoint = cfg["AZURE_WHISPER_ENDPOINT"] or cfg["AZURE_OPENAI_ENDPOINT"]
    key = cfg["AZURE_WHISPER_API_KEY"] or cfg["AZURE_OPENAI_API_KEY"]
    url = f"wss://{_azure_host(endpoint)}/openai/v1/realtime?intent=transcription"
    # Whisper realtime expects 24 kHz.
    audio24 = np.interp(np.arange(0, len(pcm), rate / 24000), np.arange(len(pcm)), pcm).astype(np.int16)
    scale = 24000 / rate
    results: dict[str, str] = {}
    async with websockets.connect(url, additional_headers={"api-key": key}, max_size=None) as ws:
        await ws.send(json.dumps({"type": "session.update", "session": {"type": "transcription", "audio": {"input": {
            "format": {"type": "audio/pcm", "rate": 24000}, "turn_detection": None,
            "transcription": {"model": cfg["AZURE_WHISPER_DEPLOYMENT"], "language": "en", "delay": cfg["AZURE_WHISPER_DELAY"]}}}}}))
        order: list[str] = []
        for a, b in segments:
            chunk = audio24[int(a * scale) : int(b * scale)].tobytes()
            for i in range(0, len(chunk), 48000):
                await ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": base64.b64encode(chunk[i : i + 48000]).decode()}))
            await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        deadline = time.time() + 60
        while len(results) < len(segments) and time.time() < deadline:
            try:
                ev = json.loads(await asyncio.wait_for(ws.recv(), 10))
            except asyncio.TimeoutError:
                break
            if ev.get("type") == "input_audio_buffer.committed":
                order.append(ev.get("item_id"))
            elif ev.get("type") == "conversation.item.input_audio_transcription.completed":
                results[ev.get("item_id")] = ev.get("transcript", "")
            elif ev.get("type") == "error":
                results[f"err{len(results)}"] = f"[error: {ev.get('error', {}).get('message')}]"
    return [results.get(i, "") for i in order] or list(results.values())


def main() -> None:
    files = sorted(RECORDINGS_DIR.glob("*.wav"))
    path = sys.argv[1] if len(sys.argv) > 1 else (str(files[-1]) if files else None)
    if not path:
        sys.exit("No recordings yet. Turn on 'Record caller audio' in the admin page and make a call.")
    cfg = settings.load()
    pcm, rate = load(path)
    segments = speech_segments(pcm, rate)
    print(f"== {path}\n")
    audio_report(pcm, rate, segments)

    if cfg["AZURE_SPEECH_REGION"] and (cfg["AZURE_SPEECH_KEY"] or cfg["AZURE_OPENAI_API_KEY"]):
        print("\n== Azure AI Speech (en-AU), no vocabulary")
        for line in azure_speech(pcm, rate, cfg):
            print("  " + line)
        print(f"\n== Azure AI Speech (en-AU), with vocabulary {vocabulary(cfg)}")
        for line in azure_speech(pcm, rate, cfg, vocabulary(cfg)):
            print("  " + line)
    else:
        print("\n== Azure AI Speech: not configured")

    print("\n== gpt-realtime-whisper (one phrase at a time, like a call)")
    if cfg["AZURE_WHISPER_DEPLOYMENT"] and (cfg["AZURE_WHISPER_ENDPOINT"] or cfg["AZURE_OPENAI_ENDPOINT"]):
        for line in asyncio.run(realtime_whisper(pcm, rate, segments, cfg)):
            print("  " + line)
    else:
        print("  (not configured)")


if __name__ == "__main__":
    main()
