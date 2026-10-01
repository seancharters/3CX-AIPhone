"""Settings managed from the admin web UI, stored in the shared /data volume.

- settings.json   all settings (API keys, 3CX extension...), read by the agent on every call
- asterisk.env    the subset Asterisk needs, as shell variables; Asterisk re-renders its
                  config and reloads whenever this file changes
- ami_secret      random password the agent uses to control Asterisk (never shown in the UI)
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .triage.prompts import DEFAULT_INSTRUCTIONS

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
SETTINGS_FILE = DATA_DIR / "settings.json"
ASTERISK_ENV_FILE = DATA_DIR / "asterisk.env"
AMI_SECRET_FILE = DATA_DIR / "ami_secret"


class ValidationError(ValueError):
    pass


def _pattern(regex: str, message: str) -> Callable[[str], str]:
    def check(value: str) -> str:
        if value and not re.fullmatch(regex, value):
            raise ValidationError(message)
        return value

    return check


def _ip(value: str) -> str:
    if value:
        try:
            ipaddress.ip_address(value)
        except ValueError as e:
            raise ValidationError("Must be an IP address, e.g. 203.0.113.10") from e
    return value


def _int_range(lo: int, hi: int) -> Callable[[str], str]:
    def check(value: str) -> str:
        if value and not (value.isdigit() and lo <= int(value) <= hi):
            raise ValidationError(f"Must be a number from {lo} to {hi}")
        return value

    return check


def _https_url(value: str) -> str:
    if value and not re.fullmatch(r"https://[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9._/-]*)?", value):
        raise ValidationError("Must be an https:// URL, e.g. https://myresource.openai.azure.com")
    return value


_EMAIL = r"[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+"


def _email_list(value: str) -> str:
    if value and not all(re.fullmatch(_EMAIL, a.strip()) for a in re.split(r"[,;]", value) if a.strip()):
        raise ValidationError("Email addresses separated by commas")
    return value


def _from_address(value: str) -> str:
    if value and not re.fullmatch(rf"({_EMAIL}|[^<>]*<{_EMAIL}>)", value):
        raise ValidationError("An email address, or Name <address>")
    return value


def _single_line(value: str) -> str:
    if any(c in value for c in "\r\n\0"):
        raise ValidationError("Must be a single line")
    return value


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    section: str
    help: str = ""
    secret: bool = False
    default: str = ""
    choices: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()  # display names for choices, same order
    multiline: bool = False  # shown as a text box; line breaks are turned into spaces
    long_text: bool = False  # large text box; line breaks kept. Stored empty while it equals the default
    validate: Callable[[str], str] = _single_line
    asterisk: bool = False  # also exported to Asterisk


FIELDS: tuple[Field, ...] = (
    # Providers
    Field("STT_PROVIDER", "Speech-to-text", "Providers", "What hears the caller", default="azure",
          choices=("azure", "azure_whisper", "elevenlabs", "deepgram"),
          labels=("Azure AI Speech", "Azure OpenAI gpt-realtime-whisper", "ElevenLabs Scribe (real-time)", "Deepgram")),
    Field("TTS_PROVIDER", "Voice", "Providers", "What speaks to the caller", default="elevenlabs",
          choices=("elevenlabs", "azure"), labels=("ElevenLabs", "Azure AI Speech")),
    Field("LLM_PROVIDER", "AI model", "Providers", "What runs the conversation and creates tickets",
          default="azure_openai", choices=("azure_openai", "anthropic"),
          labels=("Azure OpenAI", "Anthropic Claude")),
    # Azure
    Field("AZURE_OPENAI_ENDPOINT", "Azure OpenAI endpoint", "Azure",
          "From your Azure OpenAI / AI Services resource, e.g. https://myresource.openai.azure.com",
          validate=_https_url),
    Field("AZURE_OPENAI_API_KEY", "Azure OpenAI key", "Azure", secret=True),
    Field("AZURE_OPENAI_DEPLOYMENT", "Azure OpenAI deployment name", "Azure",
          "The name you gave the model deployment, e.g. gpt-4.1. Fast, non-reasoning models suit phone calls",
          validate=_pattern(r"[A-Za-z0-9._-]+", "Letters, numbers and . _ - only")),
    Field("AZURE_OPENAI_REASONING", "Reasoning effort", "Azure",
          "For reasoning models (gpt-5.x). None is fastest and Microsoft's recommendation for voice. "
          "Choose \"Don't send\" for non-reasoning models like gpt-4.1",
          default="none", choices=("none", "low", "medium", "omit"),
          labels=("None (fastest, recommended)", "Low", "Medium", "Don't send (gpt-4.1 etc.)")),
    Field("AZURE_WHISPER_ENDPOINT", "gpt-realtime-whisper endpoint", "Azure",
          "Only if gpt-realtime-whisper is selected above. Leave empty if it's the same resource as Azure OpenAI",
          validate=_https_url),
    Field("AZURE_WHISPER_API_KEY", "gpt-realtime-whisper key", "Azure",
          "Leave empty if it's the same resource as Azure OpenAI", secret=True),
    Field("AZURE_WHISPER_DEPLOYMENT", "gpt-realtime-whisper deployment name", "Azure",
          "Only if gpt-realtime-whisper is selected above",
          default="gpt-realtime-whisper",
          validate=_pattern(r"[A-Za-z0-9._-]+", "Letters, numbers and . _ - only")),
    Field("AZURE_WHISPER_DELAY", "gpt-realtime-whisper delay", "Azure",
          "Lower is faster; higher is more accurate", default="low",
          choices=("minimal", "low", "medium", "high")),
    Field("AZURE_SPEECH_REGION", "Azure Speech region", "Azure",
          "For Azure AI Speech (speech-to-text and/or voice), e.g. australiaeast",
          default="australiaeast", validate=_pattern(r"[a-z0-9]+", "Lowercase region name, e.g. australiaeast")),
    Field("AZURE_SPEECH_KEY", "Azure Speech key", "Azure",
          "Leave empty if it's the same Azure AI Services resource (and key) as Azure OpenAI", secret=True),
    Field("AZURE_TTS_VOICE", "Azure voice", "Azure",
          "Australian voices for when Voice is set to Azure AI Speech. HD voices sound much more natural "
          "but take about a second longer to start speaking. Uses the Speech region and key above",
          default="en-AU-William:DragonHDOmniLatestNeural",
          choices=('en-AU-William:DragonHDOmniLatestNeural', 'en-AU-Natasha:DragonHDOmniLatestNeural', 'en-au-siennatopaz:DragonHDOmniLatestNeural', 'en-au-cyanspark:DragonHDOmniLatestNeural', 'en-AU-Isla:MAI-Voice-2-Flash', 'en-AU-NatashaNeural', 'en-AU-WilliamNeural', 'en-AU-WilliamMultilingualNeural', 'en-AU-AnnetteNeural', 'en-AU-CarlyNeural', 'en-AU-DarrenNeural', 'en-AU-DuncanNeural', 'en-AU-ElsieNeural', 'en-AU-FreyaNeural', 'en-AU-JoanneNeural', 'en-AU-KenNeural', 'en-AU-KimNeural', 'en-AU-NeilNeural', 'en-AU-TimNeural', 'en-AU-TinaNeural'),
          labels=('William HD (male) · natural, recommended', 'Natasha HD (female) · natural', 'Sienna HD (female) · natural', 'Cyan HD (female) · natural', 'Isla MAI (female) · most expressive, preview, slower to start', 'Natasha (female) · standard, fastest', 'William (male) · standard', 'William Multilingual (male) · standard', 'Annette (female) · standard', 'Carly (female) · standard', 'Darren (male) · standard', 'Duncan (male) · standard', 'Elsie (female) · standard', 'Freya (female) · standard', 'Joanne (female) · standard', 'Ken (male) · standard', 'Kim (female) · standard', 'Neil (male) · standard', 'Tim (male) · standard', 'Tina (female) · standard')),
    # Anthropic / Deepgram
    Field("ANTHROPIC_API_KEY", "Anthropic API key", "Anthropic and Deepgram",
          "Only needed if selected above. console.anthropic.com → API Keys", secret=True),
    Field("CLAUDE_MODEL", "Claude model", "Anthropic and Deepgram", "Sonnet/Haiku respond faster; Opus is smartest",
          default="claude-opus-5", choices=("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5")),
    Field("CLAUDE_EFFORT", "Claude effort", "Anthropic and Deepgram", "Low keeps replies fast. Ignored for Haiku",
          default="low", choices=("low", "medium", "high")),
    Field("DEEPGRAM_API_KEY", "Deepgram API key", "Anthropic and Deepgram",
          "Only needed if selected above. console.deepgram.com", secret=True),
    # Voice
    Field("ELEVEN_API_KEY", "ElevenLabs API key", "Voice (ElevenLabs)",
          "Needed if Voice or Speech-to-text is ElevenLabs (both use the same credits). The key needs "
          "text-to-speech and/or speech-to-text permission. elevenlabs.io → API Keys", secret=True),
    Field("ELEVENLABS_VOICE_ID", "ElevenLabs voice ID", "Voice (ElevenLabs)",
          "Empty = Charlie (Australian). To use a Voice Library voice, first click \"Add to My Voices\" "
          "in ElevenLabs (and be on a paid plan), otherwise calls are silent",
          validate=_pattern(r"[A-Za-z0-9]+", "Letters and numbers only")),
    Field("ELEVENLABS_MODEL", "ElevenLabs model", "Voice (ElevenLabs)",
          "v4 Turbo is ElevenLabs' newest real-time model and the most natural; Flash v2.5 is marginally faster",
          default="eleven_flash_v2_5",
          choices=("eleven_v4_turbo", "eleven_flash_v2_5", "eleven_v4", "eleven_v3_conversational",
                   "eleven_turbo_v2_5", "eleven_multilingual_v2"),
          labels=("v4 Turbo · newest, natural, real-time", "Flash v2.5 · fastest",
                  "v4 · highest quality, slower to start", "v3 Conversational",
                  "Turbo v2.5", "Multilingual v2")),
    # Agent
    Field("COMPANY_NAME", "Company name", "Agent", "How the agent refers to your company", default="the IT service desk"),
    Field("GREETING", "Greeting", "Agent",
          "Exactly what the agent says when it answers. Empty = a standard greeting using the company name. "
          "Tell callers they're talking to an AI and that the call is recorded",
          multiline=True),
    Field("STT_VOCABULARY", "Custom vocabulary", "Agent",
          "Words the speech-to-text should listen for: names, client companies, products. Separate with "
          "commas, e.g. staff first names, client company names, SharePoint, FortiGate. The company name is always included. "
          "Used by Azure AI Speech and ElevenLabs Scribe (whisper doesn't support this)",
          multiline=True),
    Field("RECORD_CALLER_AUDIO", "Record caller audio (debugging)", "Agent",
          "Saves what the speech-to-text hears on each call (caller side only) so accuracy problems can be "
          "diagnosed. Keeps the last 10 calls on the server. Recordings contain callers' voices: switch off "
          "when you're done", default="no", choices=("no", "yes"), labels=("Off", "On")),
    Field("MAX_CALL_SECONDS", "Maximum call length (seconds)", "Agent", default="900",
          validate=_int_range(60, 7200)),
    Field("SYSTEM_PROMPT", "System prompt", "System prompt",
          "How the agent behaves: what it asks, priority rules, boundaries. {company} is replaced with the "
          "company name. Keep the tool names (create_ticket, transfer_to_human, end_call) so it still "
          "creates tickets, transfers and hangs up. Clear the box and save to restore the default",
          default=DEFAULT_INSTRUCTIONS, long_text=True, validate=lambda v: v),
    # Email tickets
    Field("TICKET_EMAIL_ENABLED", "Email tickets", "Email tickets",
          "Tickets are always saved on the server too. Email them to see how they'd arrive in a helpdesk inbox",
          default="no", choices=("no", "yes"), labels=("Off", "On")),
    Field("EMAIL_TO", "Send tickets to", "Email tickets", "One or more addresses, separated by commas",
          validate=_email_list),
    Field("EMAIL_FROM", "From address", "Email tickets",
          "e.g. IT Phone Agent <phone-agent@yourcompany.com.au>. Must be allowed to send via your SMTP server",
          validate=_from_address),
    Field("SMTP_HOST", "SMTP server", "Email tickets", "e.g. smtp.office365.com",
          validate=_pattern(r"[A-Za-z0-9.-]+", "A hostname, e.g. smtp.office365.com")),
    Field("SMTP_PORT", "SMTP port", "Email tickets", "587 for STARTTLS, 465 for SSL", default="587",
          validate=_int_range(1, 65535)),
    Field("SMTP_SECURITY", "Security", "Email tickets", default="starttls",
          choices=("starttls", "ssl", "none"), labels=("STARTTLS", "SSL/TLS", "None")),
    Field("SMTP_USERNAME", "SMTP username", "Email tickets", "Leave empty if the server doesn't need a login"),
    Field("SMTP_PASSWORD", "SMTP password", "Email tickets",
          "For Microsoft 365, SMTP AUTH must be enabled on the mailbox", secret=True),
    # 3CX
    Field("THREECX_HOST", "3CX FQDN", "3CX extension", "e.g. yourcompany.3cx.com.au", asterisk=True,
          validate=_pattern(r"[A-Za-z0-9.-]+", "A hostname, e.g. yourcompany.3cx.com.au")),
    Field("THREECX_PORT", "SIP port", "3CX extension", default="5060", asterisk=True,
          validate=_int_range(1, 65535)),
    Field("THREECX_EXTENSION", "Extension number", "3CX extension", "e.g. 800", asterisk=True,
          validate=_pattern(r"[0-9]+", "Digits only")),
    Field("THREECX_AUTH_ID", "Auth ID", "3CX extension",
          "From the user's IP phone settings in 3CX. Not the extension number", asterisk=True,
          validate=_pattern(r"[A-Za-z0-9._@+-]+", "Letters, numbers and . _ @ + - only")),
    Field("THREECX_PASSWORD", "Auth password", "3CX extension", secret=True, asterisk=True),
    Field("TRANSFER_TARGET", "Transfer calls to", "3CX extension",
          "3CX extension, ring group or queue for human transfers, e.g. 810. Empty = no transfers",
          validate=_pattern(r"[0-9*#]+", "Digits only")),
    Field("TRANSFER_MODE", "Transfer type", "3CX extension",
          "Announced: when the engineer answers, the agent tells them who's calling and why, then "
          "connects the caller. Blind: hands the call straight to 3CX. Use Blind for 3CX queues, which "
          "answer before an engineer picks up, so the engineer would miss the announcement",
          default="announced", choices=("announced", "blind"),
          labels=("Announced (agent briefs the engineer first)", "Blind")),
    # Server
    Field("PUBLIC_IP", "Server public IP", "Server", "So 3CX can send call audio back to this server",
          asterisk=True, validate=_ip),
    Field("TEST_SIP_PASSWORD", "Test softphone password", "Server",
          "Optional. Lets a softphone register as 'tester' and dial 800, for testing without 3CX",
          secret=True, asterisk=True),
)

FIELDS_BY_KEY = {f.key: f for f in FIELDS}


def load() -> dict[str, str]:
    """All settings, with defaults for anything unset."""
    stored: dict[str, Any] = {}
    if SETTINGS_FILE.exists():
        stored = json.loads(SETTINGS_FILE.read_text())
    return {f.key: str(stored.get(f.key) or f.default) for f in FIELDS}


def save(updates: dict[str, str]) -> dict[str, str]:
    """Validate and store updates. Returns the full new settings."""
    current = load()
    errors = []
    for key, value in updates.items():
        field = FIELDS_BY_KEY[key]
        if field.multiline:
            value = " ".join(value.split())
        value = value.strip()
        if field.long_text:
            value = value.replace("\r\n", "\n")
            if value == field.default.strip():
                value = ""  # keep tracking the built-in default
        try:
            if field.long_text:
                if len(value) > 20000:
                    raise ValidationError("Too long (maximum 20,000 characters)")
            else:
                _single_line(value)
            field.validate(value)
            if field.choices and value and value not in field.choices:
                raise ValidationError("Not an allowed option")
        except ValidationError as e:
            errors.append(f"{field.label}: {e}")
        current[key] = value
    if errors:
        raise ValidationError("; ".join(errors))

    _write_private(SETTINGS_FILE, json.dumps(current, indent=2))
    _write_private(ASTERISK_ENV_FILE, _asterisk_env(current))
    return load()


def ami_secret() -> str:
    """The Asterisk manager password, created on first use."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(AMI_SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
    except FileExistsError:
        return AMI_SECRET_FILE.read_text().strip()
    with os.fdopen(fd, "w") as f:
        f.write(secrets.token_hex(24))
    return AMI_SECRET_FILE.read_text().strip()


def _asterisk_env(values: dict[str, str]) -> str:
    lines = []
    for f in FIELDS:
        if f.asterisk:
            # Escape ";" (a comment in Asterisk config) and single-quote for the shell.
            v = values[f.key].replace(";", r"\;").replace("'", "'\\''")
            lines.append(f"{f.key}='{v}'")
    return "\n".join(lines) + "\n"


def _write_private(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content)
    tmp.chmod(0o640)
    tmp.replace(path)  # atomic, so readers never see a half-written file
