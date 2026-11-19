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
from dataclasses import dataclass, replace
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
    # Only shown in the admin UI when any of these settings has one of the listed values.
    show_if: tuple[tuple[str, tuple[str, ...]], ...] = ()
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
    # Anthropic, Deepgram
    Field("ANTHROPIC_API_KEY", "Anthropic API key", "Anthropic",
          "Only needed if selected above. console.anthropic.com → API Keys", secret=True),
    Field("CLAUDE_MODEL", "Claude model", "Anthropic", "Sonnet/Haiku respond faster; Opus is smartest",
          default="claude-opus-5", choices=("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5")),
    Field("CLAUDE_EFFORT", "Claude effort", "Anthropic", "Low keeps replies fast. Ignored for Haiku",
          default="low", choices=("low", "medium", "high")),
    Field("DEEPGRAM_API_KEY", "Deepgram API key", "Deepgram",
          "Only needed if selected above. console.deepgram.com", secret=True),
    # Voice
    Field("ELEVEN_API_KEY", "ElevenLabs API key", "ElevenLabs",
          "Needed if Voice or Speech-to-text is ElevenLabs (both use the same credits). The key needs "
          "text-to-speech and/or speech-to-text permission. elevenlabs.io → API Keys", secret=True),
    Field("ELEVENLABS_VOICE_ID", "ElevenLabs voice ID", "ElevenLabs",
          "Empty = Charlie (Australian). To use a Voice Library voice, first click \"Add to My Voices\" "
          "in ElevenLabs (and be on a paid plan), otherwise calls are silent",
          validate=_pattern(r"[A-Za-z0-9]+", "Letters and numbers only")),
    Field("ELEVENLABS_MODEL", "ElevenLabs model", "ElevenLabs",
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
    # Helpdesk (PSA)
    Field("PSA_PROVIDER", "Helpdesk (PSA)", "Helpdesk (PSA)",
          "Creates tickets in your PSA and reads its ticket number to the caller. Tickets are always saved on "
          "the server too, so if the PSA can't be reached the caller still gets a reference",
          default="none", choices=("none", "connectwise", "halopsa"),
          labels=("None: save tickets on the server (and email them if on)", "ConnectWise PSA (Manage)", "HaloPSA")),
    Field("PSA_CALLER_LOOKUP", "Recognise callers by caller ID", "Helpdesk (PSA)",
          "Looks up the caller's number in the PSA when the call is answered. If it matches a contact, the "
          "agent confirms their details instead of asking for them, and links the ticket to them. Callers who "
          "don't match are looked up by the email address they give",
          default="yes", choices=("yes", "no"), labels=("On", "Off")),
    Field("CW_SITE", "ConnectWise site", "Helpdesk (PSA)",
          "The API host for your ConnectWise cloud region, e.g. api-au.myconnectwise.net (Australia), "
          "api-na.myconnectwise.net, api-eu.myconnectwise.net, or your own server's hostname",
          validate=_pattern(r"(https://)?[A-Za-z0-9.-]+/?", "A hostname, e.g. api-au.myconnectwise.net")),
    Field("CW_COMPANY_ID", "Company ID", "Helpdesk (PSA)", "The company you enter when signing in to ConnectWise",
          validate=_pattern(r"[A-Za-z0-9_.-]+", "Letters, numbers and _ . - only")),
    Field("CW_PUBLIC_KEY", "Public key", "Helpdesk (PSA)",
          "System → Members → API Members → New. Give the member a security role that can read companies and "
          "contacts and add service tickets. Then its API Keys tab → New",
          validate=_pattern(r"[A-Za-z0-9]+", "Letters and numbers only")),
    Field("CW_PRIVATE_KEY", "Private key", "Helpdesk (PSA)", "Shown once, when the API key is created", secret=True),
    Field("CW_CLIENT_ID", "Client ID", "Helpdesk (PSA)",
          "ConnectWise requires one for every integration: developer.connectwise.com → Client IDs (free)",
          validate=_pattern(r"[0-9a-fA-F-]{36}", "A GUID, e.g. 00000000-0000-0000-0000-000000000000")),
    Field("CW_BOARD_ID", "Service board ID", "Helpdesk (PSA)",
          "Board new tickets go on. Save and test lists the boards and their IDs",
          validate=_pattern(r"[0-9]+", "Digits only")),
    Field("CW_PRIORITY_IDS", "Priority IDs for P1, P2, P3, P4", "Helpdesk (PSA)",
          "ConnectWise priority IDs in order, e.g. 6,15,8,7. Empty = the board's default priority. Save and test "
          "lists them", validate=_pattern(r"[0-9]+(,[0-9]+){3}", "Four IDs separated by commas, e.g. 6,15,8,7")),
    Field("CW_DEFAULT_COMPANY", "Company for unknown callers", "Helpdesk (PSA)",
          "Tickets from callers who aren't a ConnectWise contact go to this company: its identifier (e.g. "
          "Catchall) or record ID. The caller's details are in the ticket",
          validate=_pattern(r"[A-Za-z0-9 _.&'-]+", "A company identifier or ID")),
    Field("HALO_URL", "HaloPSA URL", "Helpdesk (PSA)", "e.g. https://yourcompany.halopsa.com", validate=_https_url),
    Field("HALO_CLIENT_ID", "Client ID", "Helpdesk (PSA)",
          "Configuration → Integrations → HaloPSA API → View Applications → New. Authentication method: Client "
          "ID and Secret (Services). Log in as an agent who can see all clients. Permissions: read:customers "
          "and edit:tickets", validate=_pattern(r"[A-Za-z0-9-]+", "Letters, numbers and - only")),
    Field("HALO_CLIENT_SECRET", "Client secret", "Helpdesk (PSA)", secret=True),
    Field("HALO_TENANT", "Tenant", "Helpdesk (PSA)",
          "Usually empty. Only if Configuration → Integrations → HaloPSA API shows a tenant name",
          validate=_pattern(r"[A-Za-z0-9_-]+", "Letters, numbers and _ - only")),
    Field("HALO_TICKET_TYPE_ID", "Ticket type ID", "Helpdesk (PSA)",
          "Empty = Halo's default ticket type. Save and test lists them", validate=_pattern(r"[0-9]+", "Digits only")),
    Field("HALO_PRIORITY_IDS", "Priority IDs for P1, P2, P3, P4", "Helpdesk (PSA)",
          "Halo priority IDs in order, e.g. 1,2,3,4. Empty = the ticket type's default. Save and test lists them",
          validate=_pattern(r"[0-9]+(,[0-9]+){3}", "Four IDs separated by commas, e.g. 1,2,3,4")),
    Field("HALO_DEFAULT_CLIENT_ID", "Client ID for unknown callers", "Helpdesk (PSA)",
          "Tickets from callers who aren't a Halo user go to this client, e.g. one called Unknown Callers. "
          "Customers → open the client → its ID is in the URL", validate=_pattern(r"[0-9]+", "Digits only")),
    # Dropped calls
    Field("DROPPED_CALL_TICKETS", "Ticket for dropped calls", "Dropped calls",
          "If a call ends before a ticket is logged (the caller hangs up or the line drops), log a P3 "
          "\"call back\" ticket with their caller ID and what they said. Not for calls the agent ended or "
          "transferred to an engineer", default="spoke", choices=("spoke", "always", "off"),
          labels=("If the caller said anything", "Every call, even hang-ups during the greeting", "Off")),
    # Password resets
    Field("PASSWORD_RESET_ENABLED", "Password resets", "Password resets",
          "Lets the agent reset Microsoft 365 / Entra passwords for callers it has verified: by employee ID "
          "if their Entra account has one, otherwise by a code sent by SMS to the mobile number on their "
          "account. Admin accounts and on-premises synced accounts are never reset by phone",
          default="no", choices=("no", "yes"), labels=("Off", "On")),
    Field("ENTRA_TENANT_ID", "Entra tenant ID", "Password resets",
          "Entra admin centre → Overview → Tenant ID",
          validate=_pattern(r"[0-9a-fA-F-]{36}|[A-Za-z0-9.-]+\.[A-Za-z]+", "A tenant ID (GUID) or domain")),
    Field("ENTRA_CLIENT_ID", "App (client) ID", "Password resets",
          "From the app registration. Application permissions (admin consented): User.Read.All, "
          "User-PasswordProfile.ReadWrite.All, RoleManagement.Read.Directory, plus GroupMember.Read.All if a "
          "group is set below, and UserAuthenticationMethod.Read.All if SMS uses MFA numbers. Also assign the "
          "app the Helpdesk Administrator role (Roles and administrators)",
          validate=_pattern(r"[0-9a-fA-F-]{36}", "A GUID, e.g. 00000000-0000-0000-0000-000000000000")),
    Field("ENTRA_CLIENT_SECRET", "Client secret", "Password resets",
          "The secret's Value (not its ID). Note when it expires", secret=True),
    Field("RESET_ALLOWED_GROUP_ID", "Only allow resets for group", "Password resets",
          "Optional. Object ID of an Entra group: only its members (including nested) can be reset by phone. "
          "Empty = any enabled, cloud-only, non-admin member account",
          validate=_pattern(r"[0-9a-fA-F-]{36}", "A group object ID (GUID)")),
    Field("SMS_PHONE_SOURCE", "SMS codes go to", "Password resets",
          "Which phone number on the account receives verification codes. Only used when the account has "
          "no employee ID", default="mobile", choices=("mobile", "mfa", "either"),
          labels=("Mobile phone on the user's profile", "Mobile number registered for MFA",
                  "Profile mobile, or MFA number if that's empty")),
    Field("SMTP2GO_API_KEY", "SMTP2GO API key", "Password resets",
          "Sends verification codes to callers whose account has no employee ID. Without it, only callers "
          "with an employee ID can be verified. SMTP2GO → Sending → API Keys; the key needs SMS access",
          secret=True),
    Field("SMS_SENDER", "SMS sender number", "Password resets",
          "Optional, in +61... format. Empty = your SMTP2GO default sender",
          validate=_pattern(r"\+[1-9][0-9]{7,14}", "International format, e.g. +61412345678")),
    Field("SMS_COUNTRY_CODE", "Default country code", "Password resets",
          "Used for numbers stored without one in Entra, e.g. 0412 345 678", default="61",
          validate=_pattern(r"[1-9][0-9]{0,2}", "Digits only, e.g. 61")),
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

STT = "STT_PROVIDER"
TTS = "TTS_PROVIDER"
LLM = "LLM_PROVIDER"
EMAIL = "TICKET_EMAIL_ENABLED"

# Which provider choices each setting belongs to, so the admin UI shows only what's in use.
SHOW_IF: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    # The Azure OpenAI endpoint/key are also whisper's fallback.
    "AZURE_OPENAI_ENDPOINT": ((LLM, ("azure_openai",)), (STT, ("azure_whisper",))),
    "AZURE_OPENAI_API_KEY": ((LLM, ("azure_openai",)), (STT, ("azure_whisper",))),
    "AZURE_OPENAI_DEPLOYMENT": ((LLM, ("azure_openai",)),),
    "AZURE_OPENAI_REASONING": ((LLM, ("azure_openai",)),),
    "AZURE_WHISPER_ENDPOINT": ((STT, ("azure_whisper",)),),
    "AZURE_WHISPER_API_KEY": ((STT, ("azure_whisper",)),),
    "AZURE_WHISPER_DEPLOYMENT": ((STT, ("azure_whisper",)),),
    "AZURE_WHISPER_DELAY": ((STT, ("azure_whisper",)),),
    "AZURE_SPEECH_REGION": ((STT, ("azure",)), (TTS, ("azure",))),
    "AZURE_SPEECH_KEY": ((STT, ("azure",)), (TTS, ("azure",))),
    "AZURE_TTS_VOICE": ((TTS, ("azure",)),),
    "ANTHROPIC_API_KEY": ((LLM, ("anthropic",)),),
    "CLAUDE_MODEL": ((LLM, ("anthropic",)),),
    "CLAUDE_EFFORT": ((LLM, ("anthropic",)),),
    "DEEPGRAM_API_KEY": ((STT, ("deepgram",)),),
    "ELEVEN_API_KEY": ((TTS, ("elevenlabs",)), (STT, ("elevenlabs",))),
    "ELEVENLABS_VOICE_ID": ((TTS, ("elevenlabs",)),),
    "ELEVENLABS_MODEL": ((TTS, ("elevenlabs",)),),
    "STT_VOCABULARY": ((STT, ("azure", "elevenlabs")),),
    **{k: (("PASSWORD_RESET_ENABLED", ("yes",)),) for k in (
        "ENTRA_TENANT_ID", "ENTRA_CLIENT_ID", "ENTRA_CLIENT_SECRET", "RESET_ALLOWED_GROUP_ID",
        "SMS_PHONE_SOURCE", "SMTP2GO_API_KEY", "SMS_SENDER", "SMS_COUNTRY_CODE")},
    "PSA_CALLER_LOOKUP": (("PSA_PROVIDER", ("connectwise", "halopsa")),),
    **{k: (("PSA_PROVIDER", ("connectwise",)),) for k in (
        "CW_SITE", "CW_COMPANY_ID", "CW_PUBLIC_KEY", "CW_PRIVATE_KEY", "CW_CLIENT_ID", "CW_BOARD_ID",
        "CW_PRIORITY_IDS", "CW_DEFAULT_COMPANY")},
    **{k: (("PSA_PROVIDER", ("halopsa",)),) for k in (
        "HALO_URL", "HALO_CLIENT_ID", "HALO_CLIENT_SECRET", "HALO_TENANT", "HALO_TICKET_TYPE_ID",
        "HALO_PRIORITY_IDS", "HALO_DEFAULT_CLIENT_ID")},
    **{k: ((EMAIL, ("yes",)),) for k in (
        "EMAIL_TO", "EMAIL_FROM", "SMTP_HOST", "SMTP_PORT", "SMTP_SECURITY", "SMTP_USERNAME", "SMTP_PASSWORD")},
}
FIELDS = tuple(replace(f, show_if=SHOW_IF.get(f.key, ())) for f in FIELDS)
FIELDS_BY_KEY = {f.key: f for f in FIELDS}
assert set(SHOW_IF) <= set(FIELDS_BY_KEY)


def visible(field: Field, values: dict[str, str]) -> bool:
    return not field.show_if or any(values.get(key) in allowed for key, allowed in field.show_if)


def load() -> dict[str, str]:
    """All settings, with defaults for anything unset."""
    stored: dict[str, Any] = {}
    if SETTINGS_FILE.exists():
        stored = json.loads(SETTINGS_FILE.read_text())
    return {f.key: str(stored.get(f.key) or f.default) for f in FIELDS}


def stored() -> dict[str, str]:
    """All settings as saved, with "" for anything left at its default (so it keeps tracking it)."""
    raw: dict[str, Any] = json.loads(SETTINGS_FILE.read_text()) if SETTINGS_FILE.exists() else {}
    return {f.key: str(raw.get(f.key) or "") for f in FIELDS}


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
