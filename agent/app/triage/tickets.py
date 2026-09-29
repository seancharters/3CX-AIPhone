"""Ticket storage.

Every ticket is saved as a JSON file under TICKETS_DIR. When email tickets are enabled in the
admin UI, it's also emailed, so you can see how a ticket would arrive in a helpdesk inbox.

To connect a real PSA/helpdesk later (HaloPSA, ConnectWise, Freshdesk...), add a class with the
same `create` method that calls its API, and return it from `get_ticket_store`.
"""

from __future__ import annotations

import asyncio
import html
import json
import os
import re
import secrets
import smtplib
import ssl
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from typing import Any, Protocol

from loguru import logger


@dataclass
class Ticket:
    caller_name: str
    organisation: str
    callback_phone: str
    email: str
    summary: str
    description: str
    affected_users: str
    category: str
    priority: str
    caller_id: str | None = None
    transcript: list[dict[str, Any]] = field(default_factory=list)


class TicketStore(Protocol):
    async def create(self, ticket: Ticket) -> str:
        """Create the ticket and return a short reference to read out to the caller."""
        ...


def _new_reference() -> str:
    # Short, phone-friendly reference: no ambiguous characters (0/O, 1/I).
    return "IT-" + "".join(secrets.choice("23456789ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(5))


class LocalTicketStore:
    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self._dir.mkdir(parents=True, exist_ok=True)

    async def create(self, ticket: Ticket) -> str:
        ref = _new_reference()
        await self.save(ref, ticket)
        return ref

    async def save(self, ref: str, ticket: Ticket) -> None:
        record = {"reference": ref, "created_at": datetime.now(timezone.utc).isoformat(), **asdict(ticket)}
        path = self._dir / f"{ref}.json"
        await asyncio.to_thread(path.write_text, json.dumps(record, indent=2, default=str))


class EmailTicketStore:
    """Saves the ticket locally, then emails it. A failed email never loses the ticket."""

    def __init__(self, cfg: dict[str, str], local: LocalTicketStore) -> None:
        self._cfg = cfg
        self._local = local

    async def create(self, ticket: Ticket) -> str:
        ref = await self._local.create(ticket)
        try:
            await asyncio.to_thread(send_email, self._cfg, ticket_email(self._cfg, ref, ticket))
            logger.info(f"Emailed ticket {ref} to {self._cfg['EMAIL_TO']}")
        except Exception:
            logger.exception(f"Couldn't email ticket {ref}; it's saved in {TICKETS_DIR}")
        return ref


TICKETS_DIR = Path(os.getenv("TICKETS_DIR", "tickets"))


def get_ticket_store(cfg: dict[str, str]) -> TicketStore:
    local = LocalTicketStore(TICKETS_DIR)
    if cfg.get("TICKET_EMAIL_ENABLED") == "yes":
        return EmailTicketStore(cfg, local)
    return local


# --- email --------------------------------------------------------------------------------


def recipients(cfg: dict[str, str]) -> list[str]:
    return [a.strip() for a in re.split(r"[,;]", cfg["EMAIL_TO"]) if a.strip()]


def send_email(cfg: dict[str, str], msg: EmailMessage) -> None:
    """Send via the SMTP server in the admin settings. Raises on failure."""
    host, port, security = cfg["SMTP_HOST"], int(cfg["SMTP_PORT"]), cfg["SMTP_SECURITY"]
    msg["From"] = cfg["EMAIL_FROM"]
    msg["To"] = ", ".join(recipients(cfg))
    context = ssl.create_default_context()
    if security == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=20, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=20)
    with server:
        server.ehlo()
        if security == "starttls":
            server.starttls(context=context)
            server.ehlo()
        if cfg["SMTP_USERNAME"]:
            server.login(cfg["SMTP_USERNAME"], cfg["SMTP_PASSWORD"])
        server.send_message(msg)


def ticket_email(cfg: dict[str, str], ref: str, t: Ticket) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = f"[{t.priority}] {ref}: {t.summary} ({t.organisation})"
    msg["Message-ID"] = make_msgid(domain=cfg["EMAIL_FROM"].rsplit("@", 1)[-1].strip("> ") or None)
    if t.email and "@" in t.email:
        msg["Reply-To"] = t.email  # replying goes to the caller

    fields = [
        ("Reference", ref),
        ("Priority", t.priority),
        ("Category", t.category.replace("_", " ")),
        ("Caller", t.caller_name),
        ("Organisation", t.organisation),
        ("Callback number", t.callback_phone),
        ("Caller ID", t.caller_id or "withheld"),
        ("Email", t.email or "not given"),
        ("Affected", t.affected_users),
    ]
    transcript = _transcript_lines(t.transcript)

    text = "\n".join(
        [f"New {t.priority} ticket from the AI phone agent", ""]
        + [f"{k}: {v}" for k, v in fields]
        + ["", "Summary", t.summary, "", "Description", t.description, "", "Call transcript"]
        + [f"{who}: {said}" for who, said in transcript]
    )
    msg.set_content(text)

    e = html.escape
    colour = {"P1": "#dc2626", "P2": "#ea580c", "P3": "#2563eb", "P4": "#64748b"}.get(t.priority, "#64748b")
    rows = "".join(
        f'<tr><td style="padding:4px 16px 4px 0;color:#64748b;white-space:nowrap">{e(k)}</td>'
        f'<td style="padding:4px 0">{e(str(v))}</td></tr>'
        for k, v in fields
    )
    lines = "".join(
        f'<p style="margin:0 0 6px"><strong>{e(who)}:</strong> {e(said)}</p>' for who, said in transcript
    )
    msg.add_alternative(
        f"""<div style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;font-size:14px;color:#1c2230;max-width:680px">
<p style="margin:0 0 12px"><span style="background:{colour};color:#fff;border-radius:4px;padding:2px 8px;font-weight:600">{e(t.priority)}</span>
<span style="color:#64748b;margin-left:8px">New ticket from the AI phone agent</span></p>
<h2 style="font-size:18px;margin:0 0 12px">{e(t.summary)}</h2>
<table style="border-collapse:collapse;margin-bottom:16px">{rows}</table>
<h3 style="font-size:15px;margin:16px 0 6px">Description</h3>
<p style="white-space:pre-wrap;margin:0">{e(t.description)}</p>
<h3 style="font-size:15px;margin:20px 0 6px">Call transcript</h3>
<div style="background:#f6f7f9;border-radius:8px;padding:12px">{lines or "<p>(none)</p>"}</div>
</div>""",
        subtype="html",
    )
    return msg


def test_email(cfg: dict[str, str]) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = "Test email from the AI phone agent"
    msg.set_content(
        "This is a test from the IT triage agent's admin page. "
        "If you can read this, email tickets are set up correctly."
    )
    return msg


def _transcript_lines(messages: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Caller/agent lines from the LLM context, skipping tool calls and internal notes."""
    lines = []
    for m in messages:
        role = m.get("role")
        if role not in ("user", "assistant"):
            continue
        content = m.get("content")
        if isinstance(content, list):
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        text = (content or "").strip()
        if not text or text.startswith(("[The call has just been answered", "[System note")):
            continue
        lines.append(("Caller" if role == "user" else "Agent", text))
    return lines
