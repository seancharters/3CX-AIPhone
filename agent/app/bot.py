"""One phone call: speech-to-text (hear) -> LLM (think, call tools) -> ElevenLabs (speak).

Speech-to-text is Azure AI Speech or Deepgram, and the LLM is Azure OpenAI or Anthropic Claude,
as chosen in the admin UI.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from fastapi import WebSocket
from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.direct_function import tool_options
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    LLMRunFrame,
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    EndWorkerFrame,
    Frame,
    FunctionCallResultProperties,
    TTSSpeakFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker, ProcessorUnusablePolicy
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.azure.llm import AzureLLMService
from pipecat.services.azure.stt import AzureSTTService
import azure.cognitiveservices.speech as speechsdk
from pipecat.services.azure.tts import AzureTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.services.elevenlabs.stt import CommitStrategy, ElevenLabsRealtimeSTTService
from pipecat.services.elevenlabs.tts import ElevenLabsHttpTTSService, ElevenLabsTTSService
import aiohttp
from pipecat.services.llm_service import FunctionCallParams, LLMService
from pipecat.services.openai.stt import OpenAIRealtimeSTTService
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService
from pipecat.transcriptions.language import Language
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport
from pipecat.utils.text.base_text_filter import BaseTextFilter
from pipecat.workers.runner import WorkerRunner

from .asterisk import ASTERISK_FRAME_BYTES, ASTERISK_SAMPLE_RATE, AMIClient, AsteriskFrameSerializer
from . import handover, settings
from .live import TranscriptTap, bus
from .recorder import CallerAudioRecorder
from .triage.prompts import greeting, instructions
from .triage.tickets import Ticket, get_ticket_store


# Always appended to the system prompt (including custom prompts from the admin UI), so the
# agent keeps using its tools correctly however the prompt is edited.
TOOL_RULES = """

# Tools (always applies)
- To log a ticket you must call the create_ticket tool. Saying you have logged it does not create it.
- Never write tool arguments, JSON or code in your replies. Everything you write is spoken aloud.
- Only read out a ticket reference that create_ticket returned to you. Never make one up. If you \
haven't called create_ticket successfully, don't give the caller a reference."""

# Appended when transfers are configured. Urgent calls should reach a person, not wait for a callback.
P1_RULES = """
- P1 issues (the whole site or business can't work, a core system is down for everyone, or a \
security incident) go straight to an engineer. As soon as it's clear the issue is P1, collect only \
the caller's name, organisation, callback number and what's down, then call create_ticket, read \
the reference, and call transfer_to_human straight away. Don't keep triaging, don't promise a \
callback, and don't ask if there's anything else first."""


class JSONSpeechFilter(BaseTextFilter):
    """Stops the voice reading out JSON, e.g. a tool call the model wrote as text.

    The JSON can span several sentences, so this tracks when it's inside an object.
    """

    def __init__(self, call_id: str = "") -> None:
        self._call_id = call_id
        self._depth = 0
        self._in_string = False
        self._escaped = False

    async def update_settings(self, settings) -> None:
        pass

    async def filter(self, text: str) -> str:
        kept: list[str] = []
        started = False
        for i, ch in enumerate(text):
            if self._depth == 0:
                # Start of a JSON object: "{" followed by a quoted key.
                if ch == "{" and text[i + 1 : i + 2].lstrip()[:1] in ('"', ""):
                    self._depth, self._in_string, self._escaped = 1, False, False
                    started = True
                else:
                    kept.append(ch)
                continue
            if self._in_string:
                if self._escaped:
                    self._escaped = False
                elif ch == "\\":
                    self._escaped = True
                elif ch == '"':
                    self._in_string = False
            elif ch == '"':
                self._in_string = True
            elif ch == "{":
                self._depth += 1
            elif ch == "}":
                self._depth -= 1
        text = "".join(kept)
        # Stage directions like "[Creating the ticket now...]" aren't meant to be spoken.
        text = re.sub(r"\[[^\]]*\]", "", text)
        if started:
            logger.warning("Model wrote JSON in its reply (likely a tool call as text); not speaking it")
            if self._call_id:
                bus.publish({"type": "note", "call_id": self._call_id,
                             "text": "⚠️ The model wrote a tool call as text instead of using the tool (not spoken)"})
        return text

    async def handle_interruption(self) -> None:
        self._depth = 0

    async def reset_interruption(self) -> None:
        self._depth = 0


@dataclass
class CallInfo:
    caller_number: str
    caller_name: str
    channel: str  # the caller's Asterisk channel, used to redirect for transfers
    codec: str = ""  # phone-line audio codec, e.g. g722
    ticket_started: bool = False  # create_ticket has been called
    ticket_id: str | None = None
    ticket_priority: str = ""
    transfer_started: bool = False  # transfer_to_human has been called


# The agent claiming a ticket is being created / exists, e.g. "I'll create a ticket for you",
# "your ticket has been logged", "the reference number is ...".
TICKET_CLAIM = re.compile(
    r"\b(creat|log|rais|lodg|open)\w*\b[^.?!]{0,40}\bticket\b"
    r"|\bticket\b[^.?!]{0,40}\b(created|logged|raised|lodged|opened)\b"
    r"|\breference (number )?is\b",
    re.IGNORECASE,
)
# The agent claiming it's transferring the caller, e.g. "I'm transferring you now", "I'll put you through".
TRANSFER_CLAIM = re.compile(
    r"\btransferr?\w*\b[^.?!]{0,30}\byou\b|\bput(ting)? you through\b|\bconnect(ing)? you (to|with)\b",
    re.IGNORECASE,
)
MAX_NUDGES = 2


def _claims(pattern: re.Pattern, text: str) -> bool:
    """True if a statement (not a question, a "before..." or a "can't") matches `pattern`."""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if sentence.rstrip().endswith("?") or re.search(
            r"\b(before|can't|cannot|can not|unable|not able|if you)\b", sentence, re.IGNORECASE
        ):
            continue
        if pattern.search(sentence):
            return True
    return False


def claims_ticket(text: str) -> bool:
    return _claims(TICKET_CLAIM, text)


def claims_transfer(text: str) -> bool:
    return _claims(TRANSFER_CLAIM, text)


class SpeechTracker(FrameProcessor):
    """Lets tools wait until the agent has finished saying something."""

    def __init__(self) -> None:
        super().__init__()
        self._started = asyncio.Event()
        self._stopped = asyncio.Event()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, BotStartedSpeakingFrame):
            self._stopped.clear()
            self._started.set()
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._started.clear()
            self._stopped.set()
        await self.push_frame(frame, direction)

    async def say_and_wait(self, llm: LLMService, text: str) -> None:
        self._started.clear()
        await llm.push_frame(TTSSpeakFrame(text))
        try:
            await asyncio.wait_for(self._started.wait(), timeout=5)
            await asyncio.wait_for(self._stopped.wait(), timeout=20)
        except TimeoutError:
            logger.warning("Timed out waiting for speech to finish")


# Voice activity detection tuned for phone audio. The defaults (0.7 confidence, 0.2s, 0.6 volume)
# miss short, quiet answers like "yes" on a phone line, and with whisper a missed "yes" isn't
# transcribed until the caller speaks again.
PHONE_VAD = VADParams(confidence=0.5, start_secs=0.1, stop_secs=0.2, min_volume=0.3)


REQUIRED_KEYS = {
    "STT_PROVIDER": {
        "azure": ["AZURE_SPEECH_REGION"],
        "azure_whisper": ["AZURE_WHISPER_DEPLOYMENT"],
        "elevenlabs": ["ELEVEN_API_KEY"],
        "deepgram": ["DEEPGRAM_API_KEY"],
    },
    "TTS_PROVIDER": {"elevenlabs": ["ELEVEN_API_KEY"], "azure": ["AZURE_SPEECH_REGION"]},
    "LLM_PROVIDER": {
        "azure_openai": ["AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT"],
        "anthropic": ["ANTHROPIC_API_KEY"],
    },
}


def _missing_settings(cfg: dict[str, str]) -> list[str]:
    keys: list[str] = []
    for provider, options in REQUIRED_KEYS.items():
        keys += options[cfg[provider]]
    # These fall back to the Azure OpenAI endpoint/key when they share a resource.
    uses_azure_speech = cfg["STT_PROVIDER"] == "azure" or cfg["TTS_PROVIDER"] == "azure"
    if uses_azure_speech and not (cfg["AZURE_SPEECH_KEY"] or cfg["AZURE_OPENAI_API_KEY"]):
        keys.append("AZURE_SPEECH_KEY")
    if cfg["STT_PROVIDER"] == "azure_whisper":
        if not (cfg["AZURE_WHISPER_ENDPOINT"] or cfg["AZURE_OPENAI_ENDPOINT"]):
            keys.append("AZURE_WHISPER_ENDPOINT")
        if not (cfg["AZURE_WHISPER_API_KEY"] or cfg["AZURE_OPENAI_API_KEY"]):
            keys.append("AZURE_WHISPER_API_KEY")
    return [settings.FIELDS_BY_KEY[k].label for k in keys if not cfg[k]]


class AzureRealtimeWhisperSTTService(OpenAIRealtimeSTTService):
    """gpt-realtime-whisper on Azure OpenAI.

    Same Realtime transcription protocol as OpenAI's, but Azure authenticates with an
    `api-key` header. https://learn.microsoft.com/azure/foundry/openai/how-to/realtime-audio-websockets
    """

    async def _websocket_connect(self, uri: str, **kwargs):
        kwargs["additional_headers"] = {"api-key": self._api_key}
        return await super()._websocket_connect(uri, **kwargs)


class AzureSTTWithVocabulary(AzureSTTService):
    """Azure AI Speech with a phrase list, which biases recognition towards known words.

    https://learn.microsoft.com/azure/ai-services/speech-service/improve-accuracy-phrase-list
    """

    def __init__(self, *, phrases: list[str], **kwargs) -> None:
        super().__init__(**kwargs)
        self._phrases = phrases

    async def _connect(self):
        # Same as the parent, plus the phrase list, which must be attached before recognition starts.
        if self._audio_stream:
            return
        try:
            stream_format = speechsdk.audio.AudioStreamFormat(samples_per_second=self.sample_rate, channels=1)
            self._audio_stream = speechsdk.audio.PushAudioInputStream(stream_format)
            self._speech_recognizer = speechsdk.SpeechRecognizer(
                speech_config=self._speech_config,
                audio_config=speechsdk.audio.AudioConfig(stream=self._audio_stream),
            )
            if self._phrases:
                grammar = speechsdk.PhraseListGrammar.from_recognizer(self._speech_recognizer)
                for phrase in self._phrases:
                    grammar.addPhrase(phrase)
            self._speech_recognizer.recognizing.connect(self._on_handle_recognizing)
            self._speech_recognizer.recognized.connect(self._on_handle_recognized)
            self._speech_recognizer.canceled.connect(self._on_handle_canceled)
            self._speech_recognizer.start_continuous_recognition_async()
        except Exception as e:
            await self.push_error(error_msg=f"Uncaught exception during initialization: {e}", exception=e)


def vocabulary(cfg: dict[str, str]) -> list[str]:
    words = [cfg["COMPANY_NAME"]] + [w.strip() for w in cfg["STT_VOCABULARY"].split(",")]
    return list(dict.fromkeys(w for w in words if w and w != "the IT service desk"))


def _azure_host(endpoint: str) -> str:
    """'https://myres.openai.azure.com/anything' -> 'myres.openai.azure.com'"""
    return endpoint.split("://", 1)[-1].split("/", 1)[0]


def _stt(cfg: dict[str, str]) -> STTService:
    if cfg["STT_PROVIDER"] == "elevenlabs":
        return ElevenLabsRealtimeSTTService(
            api_key=cfg["ELEVEN_API_KEY"],
            # ElevenLabs decides when a phrase has ended, so short answers like "yes" aren't left
            # waiting for our own speech detector.
            commit_strategy=CommitStrategy.VAD,
            settings=ElevenLabsRealtimeSTTService.Settings(language="en", keyterms=vocabulary(cfg) or None),
        )
    if cfg["STT_PROVIDER"] == "azure_whisper":
        endpoint = cfg["AZURE_WHISPER_ENDPOINT"] or cfg["AZURE_OPENAI_ENDPOINT"]
        return AzureRealtimeWhisperSTTService(
            api_key=cfg["AZURE_WHISPER_API_KEY"] or cfg["AZURE_OPENAI_API_KEY"],
            base_url=f"wss://{_azure_host(endpoint)}/openai/v1/realtime",
            settings=AzureRealtimeWhisperSTTService.Settings(
                model=cfg["AZURE_WHISPER_DEPLOYMENT"],
                language=Language.EN,
                delay=cfg["AZURE_WHISPER_DELAY"],
            ),
        )
    if cfg["STT_PROVIDER"] == "azure":
        return AzureSTTWithVocabulary(
            phrases=vocabulary(cfg),
            # One Azure AI Services resource can serve both Speech and OpenAI with the same key.
            api_key=cfg["AZURE_SPEECH_KEY"] or cfg["AZURE_OPENAI_API_KEY"],
            region=cfg["AZURE_SPEECH_REGION"],
            settings=AzureSTTService.Settings(language=Language.EN_AU),
        )
    return DeepgramSTTService(
        api_key=cfg["DEEPGRAM_API_KEY"],
        settings=DeepgramSTTService.Settings(model="nova-3", language=Language.EN_AU),
    )


# ElevenLabs' v3/v4 models only stream over HTTP, not the multi-context websocket.
ELEVENLABS_HTTP_ONLY = ("eleven_v3", "eleven_v4")


def _tts(cfg: dict[str, str], call_id: str, http: aiohttp.ClientSession) -> TTSService:
    filters = [JSONSpeechFilter(call_id)]
    if cfg["TTS_PROVIDER"] == "azure":
        return AzureTTSService(
            api_key=cfg["AZURE_SPEECH_KEY"] or cfg["AZURE_OPENAI_API_KEY"],
            region=cfg["AZURE_SPEECH_REGION"],
            settings=AzureTTSService.Settings(voice=cfg["AZURE_TTS_VOICE"], language="en-AU", effect=None),
            text_filters=filters,
        )
    if cfg["ELEVENLABS_MODEL"].startswith(ELEVENLABS_HTTP_ONLY):
        return ElevenLabsHttpTTSService(
            api_key=cfg["ELEVEN_API_KEY"],
            aiohttp_session=http,
            text_filters=filters,
            settings=ElevenLabsHttpTTSService.Settings(
                voice=cfg["ELEVENLABS_VOICE_ID"] or "IKne3meq5aSn9XLyUdCD",
                model=cfg["ELEVENLABS_MODEL"],
            ),
        )
    return ElevenLabsTTSService(
        api_key=cfg["ELEVEN_API_KEY"],
        text_filters=filters,
        settings=ElevenLabsTTSService.Settings(
            # Default is "Charlie", an Australian ElevenLabs default voice that works on the free plan.
            voice=cfg["ELEVENLABS_VOICE_ID"] or "IKne3meq5aSn9XLyUdCD",
            # eleven_flash_v2_5 is their lowest-latency model, best suited to phone calls.
            model=cfg["ELEVENLABS_MODEL"],
        ),
    )


def _llm(cfg: dict[str, str], system_prompt: str) -> LLMService:
    if cfg["LLM_PROVIDER"] == "azure_openai":
        return AzureLLMService(
            api_key=cfg["AZURE_OPENAI_API_KEY"],
            endpoint=f"https://{_azure_host(cfg['AZURE_OPENAI_ENDPOINT'])}/openai/v1",  # Azure's v1 API
            settings=AzureLLMService.Settings(
                model=cfg["AZURE_OPENAI_DEPLOYMENT"],
                system_instruction=system_prompt,
                # Reasoning models think before replying by default, which is dead air on a call.
                extra={} if cfg["AZURE_OPENAI_REASONING"] == "omit"
                else {"reasoning_effort": cfg["AZURE_OPENAI_REASONING"]},
            ),
        )

    # Haiku 4.5 has no effort setting.
    effort = "" if cfg["CLAUDE_MODEL"].startswith("claude-haiku") else cfg["CLAUDE_EFFORT"]
    extra: dict = {
        # If a safety classifier declines a turn, Anthropic retries on a fallback
        # model server-side instead of leaving the caller in silence.
        "extra_headers": {"anthropic-beta": "interleaved-thinking-2025-05-14,server-side-fallback-2026-07-01"},
        "extra_body": {"fallbacks": "default"},
    }
    if effort:
        extra["output_config"] = {"effort": effort}
    return AnthropicLLMService(
        api_key=cfg["ANTHROPIC_API_KEY"],
        settings=AnthropicLLMService.Settings(
            model=cfg["CLAUDE_MODEL"],
            system_instruction=system_prompt,
            enable_prompt_caching=True,
            extra=extra,
        ),
    )


def _tools(
    call: CallInfo, call_id: str, speech: SpeechTracker, ami: AMIClient, cfg: dict[str, str]
) -> list[FunctionSchema]:
    def note(text: str) -> None:
        bus.publish({"type": "note", "call_id": call_id, "text": text})

    tickets = get_ticket_store(cfg)
    transfer_target = cfg["TRANSFER_TARGET"]

    async def create_ticket(params: FunctionCallParams) -> None:
        if call.ticket_id:
            await params.result_callback(
                f"A ticket was already created for this call: {call.ticket_id}. Do not create another."
            )
            return
        call.ticket_started = True
        args = params.arguments
        ticket = Ticket(
            caller_name=args["caller_name"],
            organisation=args["organisation"],
            callback_phone=args["callback_phone"],
            email=args.get("email", ""),
            summary=args["summary"],
            description=args["description"],
            affected_users=args["affected_users"],
            category=args["category"],
            priority=args["priority"],
            caller_id=call.caller_number or None,
            transcript=list(params.context.get_messages()),
        )
        call.ticket_id = await tickets.create(ticket)
        call.ticket_priority = ticket.priority
        logger.info(f"Created ticket {call.ticket_id} ({ticket.priority}) for {ticket.organisation}")
        note(f"🎫 Ticket {call.ticket_id} created: {ticket.priority}, {ticket.summary}")
        if ticket.priority == "P1" and transfer_target:
            await params.result_callback(
                f"Ticket created: {call.ticket_id}. This is P1, so read the reference to the caller "
                "slowly, character by character, then call transfer_to_human straight away. Don't "
                "promise a callback or ask if there's anything else."
            )
            return
        await params.result_callback(
            f"Ticket created: {call.ticket_id}. Read the reference to the caller slowly, "
            "character by character."
        )

    def handover_text(args: dict) -> str:
        briefing = (args.get("handover") or args.get("reason") or "").strip()
        if call.ticket_id and call.ticket_id not in briefing:
            briefing += f" The ticket reference is {call.ticket_id}."
        return f"Hi, it's the AI assistant with a transfer. {briefing} Connecting you now."

    @tool_options(cancel_on_interruption=False)
    async def transfer_to_human(params: FunctionCallParams) -> None:
        call.transfer_started = True
        if not transfer_target or not call.channel:
            logger.warning("Transfer requested but 'Transfer calls to' isn't set in the admin UI")
            note("⚠️ Transfer requested, but 'Transfer calls to' isn't set in the admin page")
            await params.result_callback(
                "Transfers are not configured. Apologise, tell the caller an engineer will call them "
                "back urgently, and make sure a ticket has been created."
            )
            return

        reason = params.arguments.get("reason", "")
        announced = cfg["TRANSFER_MODE"] == "announced"
        note(f"📞 Transferring to {transfer_target}: {reason}")
        if announced:
            # Record the engineer's briefing while the caller hears this.
            text = handover_text(params.arguments)
            briefing = asyncio.create_task(handover.record(cfg, text))
            await speech.say_and_wait(
                params.llm,
                "I'm transferring you to an engineer now. I'll quickly fill them in, so please stay on the line.",
            )
            try:
                handover_id = await briefing
                note(f"🗣️ Engineer will hear: {text}")
            except Exception:
                logger.exception("Couldn't record the handover; transferring without it")
                note("⚠️ Couldn't record the engineer's briefing, so transferring without it")
                handover_id = ""
        else:
            await speech.say_and_wait(params.llm, "I'm transferring you to an engineer now. Please stay on the line.")
        logger.info(f"Transferring {call.channel} to {transfer_target}: {reason}")
        try:
            if announced and handover_id:
                # Tells the transfer dialplan to play this to the engineer before connecting the caller.
                await ami.setvar(call.channel, "TRANSFER_HANDOVER", handover_id)
            # Moves the caller's leg to the transfer dialplan, which also ends this session.
            await ami.redirect(call.channel, "agent-transfer", transfer_target)
        except Exception:
            logger.exception("Transfer failed")
            note("⚠️ Transfer failed")
            await params.result_callback(
                "The transfer failed. Apologise, tell the caller an engineer will call them back "
                "urgently, and make sure a ticket has been created."
            )
            return
        await params.result_callback("Transferred.", properties=FunctionCallResultProperties(run_llm=False))

    async def end_call(params: FunctionCallParams) -> None:
        note("Agent ended the call")
        await params.result_callback(None, properties=FunctionCallResultProperties(run_llm=False))
        # Flushes queued audio (the goodbye) before the pipeline stops and Asterisk hangs up.
        await params.llm.push_frame(EndWorkerFrame())

    return [
        FunctionSchema(
            name="create_ticket",
            description=(
                "Create the support ticket once the caller's details and the issue have been "
                "collected and confirmed with the caller. Call this exactly once per call."
            ),
            properties={
                "caller_name": {"type": "string", "description": "Caller's full name, spelling confirmed"},
                "organisation": {"type": "string", "description": "Company or organisation the caller is from"},
                "callback_phone": {"type": "string", "description": "Best number to call back on"},
                "email": {"type": "string", "description": "Caller's email address, or empty if not given"},
                "summary": {"type": "string", "description": "One-line ticket title, under 80 characters"},
                "description": {
                    "type": "string",
                    "description": "Full description: symptoms, when it started, error messages, what has "
                    "already been tried, and any other relevant detail",
                },
                "affected_users": {
                    "type": "string",
                    "description": "Who/how many are affected, e.g. 'just me', 'whole office (20)'",
                },
                "category": {
                    "type": "string",
                    "enum": ["hardware", "software", "network", "email", "account_access",
                             "security", "printing", "phone_system", "other"],
                },
                "priority": {"type": "string", "enum": ["P1", "P2", "P3", "P4"]},
            },
            required=["caller_name", "organisation", "callback_phone", "email", "summary",
                      "description", "affected_users", "category", "priority"],
            handler=create_ticket,
        ),
        FunctionSchema(
            name="transfer_to_human",
            description=(
                "Transfer the caller to a human engineer. Use when the issue is P1/critical, or when "
                "the caller asks for a person. Create the ticket first if you have enough information. "
                "This tool tells the caller they're being transferred, so don't announce it yourself."
            ),
            properties={
                "reason": {"type": "string", "description": "Why the call is being transferred"},
                "handover": {
                    "type": "string",
                    "description": "Spoken to the engineer before they're connected, in one to three short "
                    "sentences: who is calling (name and organisation), the issue and its impact, the "
                    "priority, and the ticket reference if one was created. Plain spoken English, e.g. "
                    "'I've got Jane Smith from Acme on the line. Their whole office has lost internet since "
                    "9am. I've logged it as a P1.'",
                },
            },
            required=["reason", "handover"],
            handler=transfer_to_human,
        ),
        FunctionSchema(
            name="end_call",
            description="Hang up. Only call this after you have said goodbye and the caller has nothing else.",
            properties={},
            required=[],
            handler=end_call,
        ),
    ]


async def run_call(websocket: WebSocket, call: CallInfo, ami: AMIClient) -> None:
    logger.info(f"Call from {call.caller_name!r} <{call.caller_number}> on {call.channel}")
    cfg = settings.load()  # read per call, so admin UI changes apply to the next call
    call_id = "c" + bus.new_id()
    stt_label = dict(zip(*[settings.FIELDS_BY_KEY["STT_PROVIDER"].choices, settings.FIELDS_BY_KEY["STT_PROVIDER"].labels]))
    llm_label = dict(zip(*[settings.FIELDS_BY_KEY["LLM_PROVIDER"].choices, settings.FIELDS_BY_KEY["LLM_PROVIDER"].labels]))
    bus.publish({
        "type": "call_start", "call_id": call_id, "caller": call.caller_name, "number": call.caller_number,
        "info": f"{stt_label[cfg['STT_PROVIDER']]} → {llm_label[cfg['LLM_PROVIDER']]} → "
                f"{'Azure ' + cfg['AZURE_TTS_VOICE'].split('-')[2].split(':')[0].removesuffix('Neural') if cfg['TTS_PROVIDER'] == 'azure' else 'ElevenLabs'}"
                f" · audio {call.codec or 'unknown'}",
    })
    missing = _missing_settings(cfg)
    if missing:
        logger.error(f"Can't take calls until these are set in the admin UI: {', '.join(missing)}")
        bus.publish({"type": "note", "call_id": call_id, "text": f"⚠️ Can't answer: missing {', '.join(missing)}"})
        bus.publish({"type": "call_end", "call_id": call_id})
        return

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=ASTERISK_SAMPLE_RATE,
            audio_out_sample_rate=ASTERISK_SAMPLE_RATE,
            add_wav_header=False,
            serializer=AsteriskFrameSerializer(),
            fixed_audio_packet_size=ASTERISK_FRAME_BYTES,
            session_timeout=int(cfg["MAX_CALL_SECONDS"]),
        ),
    )

    caller_ctx = (
        f"\n\nThe caller's number from caller ID is {call.caller_number}. "
        "Confirm it as the callback number rather than asking from scratch."
        if call.caller_number
        else ""
    )
    llm = _llm(cfg, instructions(cfg["COMPANY_NAME"], cfg["SYSTEM_PROMPT"]) + TOOL_RULES
               + (P1_RULES if cfg["TRANSFER_TARGET"] else "") + caller_ctx)
    stt = _stt(cfg)
    http = aiohttp.ClientSession()
    tts = _tts(cfg, call_id, http)

    speech = SpeechTracker()
    context = LLMContext(tools=_tools(call, call_id, speech, ami, cfg))
    user_agg, assistant_agg = LLMContextAggregatorPair(
        context, user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer(params=PHONE_VAD))
    )

    recorder = CallerAudioRecorder(call.caller_number or call.caller_name) if cfg["RECORD_CALLER_AUDIO"] == "yes" else None
    pipeline = Pipeline(
        [
            transport.input(),
            *([recorder] if recorder else []),
            stt,
            TranscriptTap(call_id, "caller"),
            user_agg,
            llm,
            tts,
            transport.output(),
            TranscriptTap(call_id, "agent"),
            speech,
            assistant_agg,
        ]
    )
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True),
        # If a service fails (bad key, outage), hang up rather than leave dead air.
        processor_unusable_policy=ProcessorUnusablePolicy.END,
    )
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)

    nudges = 0
    reported_errors: set[str] = set()

    @worker.event_handler("on_pipeline_error")
    async def on_pipeline_error(worker, frame):
        # Surface service problems (e.g. voice out of credits) in the Live calls tab, once each.
        error = str(frame.error)
        if "does not exist" in error and "voice_id" in error:
            error = "ElevenLabs doesn't recognise this voice ID. Add it to My Voices in ElevenLabs first"
        elif "completed with no audio" in error or "connection failed" in error:
            error = ("The voice service returned no audio: out of credits, the voice isn't in your "
                     "ElevenLabs My Voices, or the key was rejected")
        name = type(frame.processor).__name__ if frame.processor else "Pipeline"
        text = f"⚠️ {name}: {error[:200]}"
        if text not in reported_errors:
            reported_errors.add(text)
            bus.publish({"type": "note", "call_id": call_id, "text": text})

    @assistant_agg.event_handler("on_assistant_turn_stopped")
    async def on_assistant_turn_stopped(aggregator, message):
        # Some models say "I'll create a ticket for you" or "I'm transferring you now" and then don't
        # call the tool (later inventing a reference, or leaving the caller on hold). Catch that and
        # tell the model to actually call it.
        text = message.content or ""
        if not call.ticket_started and claims_ticket(text):
            asyncio.create_task(nudge("ticket"))
        elif not call.transfer_started and claims_transfer(text):
            asyncio.create_task(nudge("transfer"))
        elif call.ticket_priority == "P1" and cfg["TRANSFER_TARGET"] and not call.transfer_started:
            asyncio.create_task(nudge("p1"))

    async def nudge(kind: str):
        nonlocal nudges
        await asyncio.sleep(1.5)  # the same reply may still be calling the tool
        done = call.ticket_started if kind == "ticket" else call.transfer_started
        if done or nudges >= MAX_NUDGES:
            return
        nudges += 1
        if kind == "ticket":
            what, instruction = "creating a ticket", (
                "You told the caller you are creating or have created a ticket, but you have not called "
                "create_ticket. Call create_ticket now with the details you have collected, then read the "
                "caller the reference it returns."
            )
        elif kind == "p1":
            what, instruction = "", (
                "This is a P1 ticket, so the caller must be transferred to an engineer now. Call "
                "transfer_to_human now. Don't say anything else first."
            )
        else:
            what, instruction = "transferring the caller", (
                "You told the caller you are transferring them, but you have not called transfer_to_human. "
                "Call transfer_to_human now. Don't say anything else first."
            )
        if kind == "p1":
            logger.warning("Agent carried on after a P1 ticket without transferring; prompting it")
            bus.publish({"type": "note", "call_id": call_id,
                         "text": "⚠️ P1 ticket but the agent didn't transfer; told it to transfer now"})
        else:
            logger.warning(f"Agent said it was {what} without calling the tool; prompting it")
            bus.publish({"type": "note", "call_id": call_id,
                         "text": f"⚠️ Agent said it was {what} without doing it; told it to do it now"})
        context.add_message({"role": "user", "content": f"[System note, not from the caller: {instruction}]"})
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        # Speak the greeting word for word (no LLM round trip, so the caller hears it immediately).
        # It's added to the context as the agent's first turn.
        context.add_message({"role": "user", "content": "[The call has just been answered.]"})
        await worker.queue_frames([TTSSpeakFrame(greeting(cfg["COMPANY_NAME"], cfg["GREETING"]))])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info(f"Call ended: {call.channel}")
        await runner.cancel()

    try:
        await runner.run()
    finally:
        await http.close()
        bus.publish({"type": "call_end", "call_id": call_id})
        if recorder:
            await recorder.save()
