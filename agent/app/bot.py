"""One phone call: speech-to-text (hear) -> LLM (think, call tools) -> ElevenLabs (speak).

Speech-to-text is Azure AI Speech or Deepgram, and the LLM is Azure OpenAI or Anthropic Claude,
as chosen in the admin UI.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field

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
    FunctionCallsStartedFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
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
from . import handover, identity, settings
from .live import TranscriptTap, bus
from .recorder import CallerAudioRecorder
from .triage.prompts import RESET_RULES, greeting, instructions
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
    reset: identity.ResetSession = field(default_factory=identity.ResetSession)


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
# The agent saying it's about to do something a tool does ("Now I will look up your account",
# "I'll send a code to your mobile") or stalling while it supposedly does it ("One moment please").
# If no tool call follows, the caller is left in silence.
STALL_CLAIM = re.compile(
    r"\b(look|search|find|check|pull|locat)\w*\b[^.?!]{0,20}\b(your|the|that|this)\s+(account|email|details|user)\b"
    r"|\b(send|text)\w*\b[^.?!]{0,40}\bcode\b"
    r"|\breset\w*\b[^.?!]{0,20}\bpassword\b[^.?!]{0,10}\bnow\b"
    r"|\b(one|a|just a) (moment|sec|second|minute)\b|\bbear with me\b|\bplease (hold|wait)\b|\bhang on\b",
    re.IGNORECASE,
)
MAX_NUDGES = 2


def _claims(pattern: re.Pattern, text: str) -> bool:
    """True if a statement (not a question, a "before..." or a "can't") matches `pattern`."""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if sentence.rstrip().endswith("?") or re.search(
            r"\b(before|can't|cannot|can not|couldn't|could not|unable|not able|wasn't able|"
            r"didn't|if you)\b", sentence, re.IGNORECASE
        ):
            continue
        if pattern.search(sentence):
            return True
    return False


def claims_ticket(text: str) -> bool:
    return _claims(TICKET_CLAIM, text)


def claims_transfer(text: str) -> bool:
    return _claims(TRANSFER_CLAIM, text)


def claims_stall(text: str) -> bool:
    return _claims(STALL_CLAIM, text)


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

    async def say_and_wait(self, llm: LLMService, text: str, append_to_context: bool = True) -> None:
        self._started.clear()
        await llm.push_frame(TTSSpeakFrame(text, append_to_context=append_to_context))
        try:
            await asyncio.wait_for(self._started.wait(), timeout=5)
            await asyncio.wait_for(self._stopped.wait(), timeout=20)
        except TimeoutError:
            logger.warning("Timed out waiting for speech to finish")


class ReplyWatcher(FrameProcessor):
    """Sits right after the LLM. Calls `on_stall(text)` when a reply says it's about to do
    something ("I'll send a code... One moment please") but the reply made no tool call, which
    would otherwise leave the caller in silence."""

    def __init__(self, on_stall) -> None:
        super().__init__()
        self._on_stall = on_stall
        self._text: list[str] = []
        self._called_tool = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMFullResponseStartFrame):
            self._text, self._called_tool = [], False
        elif isinstance(frame, LLMTextFrame):
            self._text.append(frame.text)
        elif isinstance(frame, FunctionCallsStartedFrame):
            self._called_tool = True
        elif isinstance(frame, LLMFullResponseEndFrame):
            text = "".join(self._text)
            if not self._called_tool and claims_stall(text):
                await self._on_stall(text)
        await self.push_frame(frame, direction)


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

MAX_LOOKUPS = 5  # account lookups per call, so the agent can't be used to probe the directory
PASSWORD_REDACTED = "🔒 [temporary password read out: hidden]"


def reset_configured(cfg: dict[str, str]) -> list[str]:
    """Missing settings for password resets, or [] if they're ready."""
    # SMTP2GO is optional: without it, only callers with an employee ID can be verified.
    keys = ["ENTRA_TENANT_ID", "ENTRA_CLIENT_ID", "ENTRA_CLIENT_SECRET"]
    return [settings.FIELDS_BY_KEY[k].label for k in keys if not cfg[k]]


def _reset_tools(
    call: CallInfo, note, speech: SpeechTracker, cfg: dict[str, str], taps: list[TranscriptTap]
) -> list[FunctionSchema]:
    """Caller verification and password reset tools. All the checks are here, not in the prompt."""
    s = call.reset
    graph = identity.GraphClient(cfg)
    sms_enabled = bool(cfg["SMTP2GO_API_KEY"])
    lookups = 0
    unavailable = ("The password reset system isn't available right now. Apologise, and offer to log a "
                   "ticket or transfer them to an engineer.")
    refuse = "Don't reset anything. Offer to log a ticket, or transfer them to an engineer."

    def who() -> dict:
        u = s.user or {}
        return {"user": u.get("userPrincipalName"), "user_id": u.get("id"), "caller_id": call.caller_number}

    def blocked_reason() -> str:
        if s.reset_done:
            return "A password has already been reset on this call. Only one reset is allowed per call."
        if s.blocked:
            return f"Verification has failed too many times on this call. {refuse}"
        return ""

    async def account_mobile(u: dict) -> str:
        source = cfg["SMS_PHONE_SOURCE"]
        raw = (u.get("mobilePhone") or "") if source in ("mobile", "either") else ""
        if not raw and source in ("mfa", "either"):
            raw = await identity.mfa_mobile(graph, u["id"])
        return identity.to_e164(raw, cfg["SMS_COUNTRY_CODE"])

    def verified_result() -> str:
        if s.sms_number:
            return ("Verified. Confirm the caller wants their password reset now, and ask whether they'd like "
                    "the new temporary password read out now, or sent by text message to the mobile on their "
                    f"account ending in {', '.join(s.sms_number[-3:])}. Then call reset_password with their choice.")
        return ("Verified. Confirm the caller wants their password reset now, then call reset_password with "
                "delivery read_out. It can't be texted: there's no mobile number on their account.")

    async def find_user_account(params: FunctionCallParams) -> None:
        nonlocal lookups
        if reason := blocked_reason():
            await params.result_callback(reason)
            return
        lookups += 1
        if lookups > MAX_LOOKUPS:
            await params.result_callback(f"Too many account lookups on this call. {refuse}")
            return
        query = str(params.arguments.get("email_or_name", "")).strip()
        try:
            users = await identity.find_users(graph, query) if len(query) >= 3 else []
        except identity.IdentityError as err:
            logger.error(f"Account lookup failed: {err}")
            note(f"⚠️ Entra lookup failed: {err}")
            await params.result_callback(unavailable)
            return
        if not users:
            await params.result_callback(
                "No account matches that. Check the spelling with the caller (read it back), or ask for "
                "their work email address, and try again.")
            return
        if len(users) > 1:
            await params.result_callback(
                "Several accounts match that name. Ask for their work email address and look it up with that. "
                "Don't read out any names.")
            return
        u = users[0]
        s.user, s.verified = None, False  # nothing is selected until every check passes
        try:
            if not u.get("accountEnabled", False):
                problem = "This account is disabled, so it can't be reset by phone."
            elif u.get("onPremisesSyncEnabled"):
                problem = ("This account is managed by the on-premises directory, so it can't be reset by "
                           "phone.")
            elif await identity.has_admin_role(graph, u["id"]):
                problem = "This account has administrator privileges, so for security it can't be reset by phone."
            elif cfg["RESET_ALLOWED_GROUP_ID"] and not await identity.in_group(graph, u["id"], cfg["RESET_ALLOWED_GROUP_ID"]):
                problem = "This account isn't set up for phone password resets."
            elif identity.locked_out(u["id"]) or identity.too_many_resets(u["id"]):
                problem = "Phone password resets for this account are paused for now after recent attempts."
            else:
                problem = ""
            # The account's mobile: where verification codes go, and where the temporary password can be
            # texted. Never a number the caller gives.
            number = ""
            if not problem and sms_enabled:
                try:
                    number = await account_mobile(u)
                except identity.IdentityError:
                    if not u.get("employeeId"):
                        raise  # SMS is the only way to verify them
                    logger.warning("Couldn't read the account's mobile; temporary password can only be read out")
            if not problem and not u.get("employeeId") and not number:
                problem = "There's no way to verify this caller by phone: no employee ID or mobile number on the account."
        except identity.IdentityError as err:
            logger.error(f"Account checks failed: {err}")
            note(f"⚠️ Entra check failed: {err}")
            await params.result_callback(unavailable)
            return
        name = u.get("displayName") or "the caller"
        if problem:
            identity.audit("lookup_refused", user=u.get("userPrincipalName"), user_id=u["id"],
                           reason=problem, caller_id=call.caller_number)
            note(f"🔐 Password reset not allowed for {u.get('userPrincipalName')}: {problem}")
            await params.result_callback(f"{problem} Tell the caller that. {refuse}")
            return
        method = "employee_id" if u.get("employeeId") else "sms"
        s.select(u, method, number)
        identity.audit("lookup", method=method, **who())
        note(f"🔐 Password reset: found {u.get('userPrincipalName')}, verifying by "
             + ("employee ID" if method == "employee_id" else f"SMS to …{number[-3:]}"))
        if method == "employee_id":
            await params.result_callback(
                f"Found the account for {name}. Check with the caller that this is them. To verify them, ask "
                "for their employee ID number, then call verify_employee_id with it.")
        else:
            await params.result_callback(
                f"Found the account for {name}. Check with the caller that this is them. To verify them, a "
                f"six-digit code will be sent by SMS to the mobile number on their account, ending in "
                f"{', '.join(number[-3:])}. Tell them, then call send_verification_code.")

    async def verify_employee_id(params: FunctionCallParams) -> None:
        if reason := blocked_reason():
            await params.result_callback(reason)
            return
        if not s.user or s.method != "employee_id":
            await params.result_callback("Employee ID verification isn't available. Look the account up first "
                                         "with find_user_account and follow what it says.")
            return
        if s.verified:
            await params.result_callback(verified_result())
            return
        if s.check_employee_id(str(params.arguments.get("employee_id", ""))):
            s.verified = True
            identity.audit("verified", method="employee_id", **who())
            note("✅ Caller verified by employee ID")
            await params.result_callback(verified_result())
            return
        identity.record_failure(s.user["id"])
        identity.audit("verify_failed", method="employee_id", attempt=s.id_attempts, **who())
        left = identity.MAX_ID_ATTEMPTS - s.id_attempts
        if left <= 0:
            s.blocked = True
            note("⛔ Employee ID didn't match 3 times: no reset on this call")
            await params.result_callback(
                "That doesn't match, and that was the last attempt. Tell the caller you can't verify them "
                f"over the phone. Never hint at the correct number. {refuse}")
            return
        note(f"❌ Employee ID didn't match ({left} attempt{'s' if left > 1 else ''} left)")
        await params.result_callback(
            f"That doesn't match the account. Ask them to say it again carefully. {left} attempt"
            f"{'s' if left > 1 else ''} left. Never hint at the correct number.")

    async def send_verification_code(params: FunctionCallParams) -> None:
        if reason := blocked_reason():
            await params.result_callback(reason)
            return
        if not s.user or s.method != "sms":
            await params.result_callback("SMS verification isn't available for this caller. Follow what "
                                         "find_user_account told you.")
            return
        if s.verified:
            await params.result_callback(verified_result())
            return
        if s.sms_sends >= identity.MAX_SMS_SENDS:
            await params.result_callback(f"No more codes can be sent on this call. {refuse}")
            return
        code = s.new_code()
        text = (f"Your IT support verification code is {code}. It expires in 10 minutes. If you didn't "
                "call IT support, don't share it, and let your IT team know.")
        try:
            await identity.send_sms(cfg, s.sms_number, text)
        except identity.IdentityError as err:
            s.code = ""
            logger.error(f"Verification SMS failed: {err}")
            note(f"⚠️ SMS failed: {err}")
            await params.result_callback(f"The text message couldn't be sent. Apologise. {refuse}")
            return
        identity.audit("code_sent", to_ending=s.sms_number[-3:], **who())
        note(f"📱 Verification code sent to …{s.sms_number[-3:]}")
        await params.result_callback(
            "Code sent. Ask the caller to read out the six-digit code when it arrives, then call "
            "verify_sms_code with it. It's valid for 10 minutes.")

    async def verify_sms_code(params: FunctionCallParams) -> None:
        if reason := blocked_reason():
            await params.result_callback(reason)
            return
        if not s.user or s.method != "sms" or not s.code:
            await params.result_callback("No code has been sent. Follow what find_user_account told you.")
            return
        if s.verified:
            await params.result_callback(verified_result())
            return
        if time.time() > s.code_expires:
            s.code = ""
            await params.result_callback("That code has expired. Offer to send a new one with send_verification_code.")
            return
        if s.check_code(str(params.arguments.get("code", ""))):
            s.verified, s.code = True, ""
            identity.audit("verified", method="sms", **who())
            note("✅ Caller verified by SMS code")
            await params.result_callback(verified_result())
            return
        identity.record_failure(s.user["id"])
        identity.audit("verify_failed", method="sms", attempt=s.code_attempts, **who())
        left = identity.MAX_CODE_ATTEMPTS - s.code_attempts
        if left > 0:
            note(f"❌ SMS code didn't match ({left} attempt{'s' if left > 1 else ''} left)")
            await params.result_callback(
                f"That code doesn't match. Ask them to read it again, digit by digit. {left} attempt"
                f"{'s' if left > 1 else ''} left.")
            return
        s.code = ""
        if s.sms_sends < identity.MAX_SMS_SENDS:
            note("❌ SMS code didn't match 3 times; code cancelled")
            await params.result_callback(
                "That code doesn't match, and it has now been cancelled. You can offer to send one new code "
                "with send_verification_code.")
            return
        s.blocked = True
        note("⛔ SMS verification failed: no reset on this call")
        await params.result_callback(f"That code doesn't match, and that was the last attempt. {refuse}")

    async def speak_password(llm: LLMService) -> None:
        # Spoken straight to the caller and kept out of the model's context, so it can't be repeated
        # into a ticket or transcript.
        spoken = identity.spoken_password(s.temp_password)
        await speech.say_and_wait(
            llm, f"Your temporary password is {spoken} Once more, that's {spoken}", append_to_context=False)

    @tool_options(cancel_on_interruption=False)
    async def reset_password(params: FunctionCallParams) -> None:
        if s.reset_done:
            await params.result_callback("The password was already reset on this call. If they missed the "
                                         "temporary password, call repeat_temporary_password.")
            return
        if not s.user or not s.verified or s.blocked:
            await params.result_callback("The caller hasn't been verified, so the password can't be reset. "
                                         "Follow the verification steps first.")
            return
        if identity.too_many_resets(s.user["id"]):
            await params.result_callback(f"This account has been reset too many times today. {refuse}")
            return
        password = identity.temporary_password()
        try:
            try:
                await identity.set_password(graph, s.user["id"], password)
            except identity.IdentityError as err:
                if "Password" not in str(err) or "403" in str(err):
                    raise
                password = identity.temporary_password()  # rejected by the password policy; try another
                await identity.set_password(graph, s.user["id"], password)
        except identity.IdentityError as err:
            logger.error(f"Password reset failed: {err}")
            identity.audit("reset_failed", error=str(err)[:300], **who())
            note(f"⚠️ Password reset failed: {err}")
            await params.result_callback(f"The reset didn't work. Apologise. {refuse}")
            return
        s.reset_done, s.temp_password = True, password
        identity.record_reset(s.user["id"])
        words = password.split("-")
        s.redact += [password, *words]
        for tap in taps:
            tap.redact(words[:2], PASSWORD_REDACTED)
        upn = s.user.get("userPrincipalName")
        after = ("Tell them to sign in with it now, and that they'll be asked to choose a new password straight "
                 "away. Then create the ticket.")

        if params.arguments.get("delivery") == "sms" and s.sms_number:
            try:
                await identity.send_sms(cfg, s.sms_number, (
                    f"Your temporary password is:\n{password}\n\nYou'll be asked to choose a new password when "
                    "you sign in. If you didn't ask IT support for this, call them straight away."))
            except identity.IdentityError as err:
                logger.error(f"Temporary password SMS failed: {err}")
                note(f"⚠️ Couldn't text the temporary password ({err}); reading it out instead")
                identity.audit("reset", delivery="read_out_after_sms_failed", **who())
                await speech.say_and_wait(
                    params.llm, "Your password has been reset, but I couldn't send the text message, so I'll "
                    "read it out instead.", append_to_context=False)
                await speak_password(params.llm)
                await params.result_callback(
                    "The text message failed, so the system read the temporary password to the caller twice "
                    "instead. You can't see it, so never try to say it. If they didn't catch it, call "
                    f"repeat_temporary_password. {after}")
                return
            identity.audit("reset", delivery="sms", to_ending=s.sms_number[-3:], **who())
            note(f"🔑 Password reset for {upn}. Temporary password texted to …{s.sms_number[-3:]} (not shown); "
                 "they must change it at sign-in")
            await params.result_callback(
                f"The password has been reset and the temporary password has been texted to their mobile ending "
                f"in {', '.join(s.sms_number[-3:])}. You can't see it, so never try to say it. Ask them to check "
                "it arrived. If it doesn't arrive, call repeat_temporary_password to read it out instead. "
                + after)
            return

        identity.audit("reset", delivery="read_out", **who())
        note(f"🔑 Password reset for {upn}. Temporary password read to the caller (not shown); they must "
             "change it at sign-in")
        await speech.say_and_wait(params.llm, "All done, your password has been reset.", append_to_context=False)
        await speak_password(params.llm)
        await params.result_callback(
            "The password has been reset and the system has already read the temporary password to the caller "
            "twice. You can't see it, so never try to say it. If they didn't catch it, call "
            f"repeat_temporary_password. {after}")

    @tool_options(cancel_on_interruption=False)
    async def repeat_temporary_password(params: FunctionCallParams) -> None:
        if not s.reset_done or not s.temp_password:
            await params.result_callback("No password has been reset on this call.")
            return
        if s.repeats >= 2:
            await params.result_callback(
                "It's been repeated enough times. If they still can't get it, offer to transfer them to an engineer.")
            return
        s.repeats += 1
        await speak_password(params.llm)
        await params.result_callback("Repeated. Don't say the password yourself.")

    return [
        FunctionSchema(
            name="find_user_account",
            description="Start a password reset: find the caller's Microsoft 365 account and learn how to "
                        "verify them. Call this only when the caller wants their own password reset.",
            properties={"email_or_name": {
                "type": "string",
                "description": "The caller's work email address (preferred), or their full name"}},
            required=["email_or_name"],
            handler=find_user_account,
        ),
        FunctionSchema(
            name="verify_employee_id",
            description="Check the employee ID the caller gave against their account.",
            properties={"employee_id": {
                "type": "string", "description": "Exactly what the caller said, as digits/letters, no spaces"}},
            required=["employee_id"],
            handler=verify_employee_id,
        ),
        FunctionSchema(
            name="send_verification_code",
            description="Text a verification code to the mobile number on the caller's account. Takes no "
                        "number: it only ever goes to the number on the account.",
            properties={},
            required=[],
            handler=send_verification_code,
        ),
        FunctionSchema(
            name="verify_sms_code",
            description="Check the code the caller read out from the text message.",
            properties={"code": {"type": "string", "description": "The six digits the caller read out"}},
            required=["code"],
            handler=verify_sms_code,
        ),
        FunctionSchema(
            name="reset_password",
            description="Reset the verified caller's password. Delivers the temporary password itself, read "
                        "out or by text to the mobile on their account, as the caller chose. Only works after "
                        "verification.",
            properties={"delivery": {
                "type": "string", "enum": ["read_out", "sms"],
                "description": "How the caller chose to receive the temporary password"}},
            required=["delivery"],
            handler=reset_password,
        ),
        FunctionSchema(
            name="repeat_temporary_password",
            description="Read the temporary password out to the caller, if they didn't catch it or the text "
                        "didn't arrive.",
            properties={},
            required=[],
            handler=repeat_temporary_password,
        ),
    ]



def _tools(
    call: CallInfo, call_id: str, speech: SpeechTracker, ami: AMIClient, cfg: dict[str, str],
    taps: list[TranscriptTap],
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
            transcript=identity.redact_transcript(list(params.context.get_messages()), call.reset.redact),
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

    reset_tools = (
        _reset_tools(call, note, speech, cfg, taps)
        if cfg["PASSWORD_RESET_ENABLED"] == "yes" and not reset_configured(cfg) else []
    )
    return reset_tools + [
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
    resets_on = cfg["PASSWORD_RESET_ENABLED"] == "yes"
    if resets_on and (missing_reset := reset_configured(cfg)):
        resets_on = False
        logger.warning(f"Password resets are on but not set up: missing {', '.join(missing_reset)}")
        bus.publish({"type": "note", "call_id": call_id,
                     "text": f"⚠️ Password resets are off for this call: missing {', '.join(missing_reset)}"})
    llm = _llm(cfg, instructions(cfg["COMPANY_NAME"], cfg["SYSTEM_PROMPT"]) + TOOL_RULES
               + (P1_RULES if cfg["TRANSFER_TARGET"] else "") + (RESET_RULES if resets_on else "")
               + caller_ctx)
    stt = _stt(cfg)
    http = aiohttp.ClientSession()
    tts = _tts(cfg, call_id, http)

    speech = SpeechTracker()
    caller_tap, agent_tap = TranscriptTap(call_id, "caller"), TranscriptTap(call_id, "agent")
    context = LLMContext(tools=_tools(call, call_id, speech, ami, cfg, [caller_tap, agent_tap]))
    user_agg, assistant_agg = LLMContextAggregatorPair(
        context, user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer(params=PHONE_VAD))
    )

    async def on_stall(text: str) -> None:
        if not claims_ticket(text) and not claims_transfer(text):  # those have their own nudges
            asyncio.create_task(nudge("stall"))

    reply_watcher = ReplyWatcher(on_stall)
    recorder = CallerAudioRecorder(call.caller_number or call.caller_name) if cfg["RECORD_CALLER_AUDIO"] == "yes" else None
    pipeline = Pipeline(
        [
            transport.input(),
            *([recorder] if recorder else []),
            stt,
            caller_tap,
            user_agg,
            llm,
            reply_watcher,
            tts,
            transport.output(),
            agent_tap,
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

    nudges: dict[str, int] = {}
    caller_turns = 0
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

    @user_agg.event_handler("on_user_turn_started")
    async def on_user_turn_started(aggregator, *args):
        nonlocal caller_turns
        caller_turns += 1

    async def nudge(kind: str):
        turns_before = caller_turns
        await asyncio.sleep(1.5)  # the same reply may still be calling the tool
        if kind == "stall":
            done = caller_turns != turns_before  # the caller spoke; let the model answer them instead
        else:
            done = call.ticket_started if kind == "ticket" else call.transfer_started
        if done or nudges.get(kind, 0) >= MAX_NUDGES:
            return
        nudges[kind] = nudges.get(kind, 0) + 1
        if kind == "stall":
            what, instruction = "doing something (\"one moment\")", (
                "You told the caller you are about to do something (for example looking up their account, "
                "sending a verification code, or resetting their password) and asked them to wait, but you "
                "did not call a tool, so nothing is happening and the caller is waiting in silence. Call the "
                "tool for that step now. Don't say anything else first."
            )
        elif kind == "ticket":
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
