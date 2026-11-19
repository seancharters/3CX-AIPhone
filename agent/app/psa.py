"""Helpdesk (PSA) integrations: ConnectWise PSA (Manage) and HaloPSA.

Two jobs:
- Contact lookup: match the caller ID (or the email the caller gives) to a contact, so the ticket
  is linked to the right contact and company, and the agent can confirm details instead of
  asking for them.
- Ticket creation: the ticket is created in the PSA and its number is read to the caller. It's
  always saved on the server first, so a PSA outage never loses a ticket.

Lookups only ever return exact matches (a phone number's last 8 digits, or the whole email
address), checked here in code after the PSA's own search, so the agent can't be used to browse
the contact list.
"""

from __future__ import annotations

import asyncio
import base64
import html
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger

TIMEOUT = httpx.Timeout(10, connect=5)
PRIORITIES = ("P1", "P2", "P3", "P4")


class PSAError(Exception):
    """Something went wrong talking to the PSA. The message is safe to log and show to admins."""


@dataclass
class Contact:
    id: str
    name: str
    company_id: str
    company_name: str
    email: str = ""
    site_id: str = ""  # HaloPSA only


@dataclass
class TicketRequest:
    summary: str
    description: str  # what the customer would see
    internal_note: str  # call transcript etc., hidden from the customer
    priority: str  # P1..P4
    caller_name: str
    callback_phone: str
    email: str
    contact: Contact | None  # linked contact, if known
    company_id: str = ""  # company to log it against when there's no contact (e.g. from caller ID)


def digits(number: str) -> str:
    return re.sub(r"\D", "", number or "")


def same_phone(a: str, b: str) -> bool:
    """Same number, ignoring formatting and country code. Compares the last 9 digits (an Australian
    number without its 0 or +61), or the last 8 when one was stored without its area code. Only 8
    would confuse e.g. mobile 0412 345 678 with landline (02) 1234 5678."""
    da, db = digits(a), digits(b)
    if len(da) < 8 or len(db) < 8:
        return False
    n = 9 if len(da) >= 9 and len(db) >= 9 else 8
    return da[-n:] == db[-n:]


_EMAIL = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def clean_email(value: str) -> str:
    value = (value or "").strip().lower()
    return value if _EMAIL.fullmatch(value) else ""


def priority_ids(mapping: str) -> dict[str, str]:
    """'1,2,3,4' -> {'P1': '1', ...}; '' -> {} (let the PSA use its default)."""
    ids = [p.strip() for p in (mapping or "").split(",")]
    return dict(zip(PRIORITIES, ids)) if len(ids) == 4 and all(ids) else {}


def _error_text(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return r.text[:300]
    if isinstance(body, dict):
        parts = [str(body.get(k)) for k in ("message", "error_description", "error", "ClassName") if body.get(k)]
        for err in body.get("errors") or []:
            if isinstance(err, dict):
                parts.append(f"{err.get('field', '')} {err.get('message', '')}".strip())
        return "; ".join(parts)[:300] or str(body)[:300]
    return str(body)[:300]


class PSA:
    name = ""

    async def find_by_phone(self, number: str) -> list[Contact]:
        raise NotImplementedError

    async def find_by_email(self, email: str) -> list[Contact]:
        raise NotImplementedError

    async def create_ticket(self, t: TicketRequest) -> str:
        """Create the ticket and return its number."""
        raise NotImplementedError

    async def test(self, lookup_number: str = "") -> str:
        """Check the credentials and list the IDs needed for setup."""
        raise NotImplementedError


# --- ConnectWise PSA (Manage) ------------------------------------------------------------


class ConnectWise(PSA):
    name = "ConnectWise"
    _codebases: dict[str, str] = {}  # site/company -> e.g. "v4_6_release/"

    def __init__(self, cfg: dict[str, str]) -> None:
        self.site = cfg["CW_SITE"].removeprefix("https://").strip("/")
        self.company = cfg["CW_COMPANY_ID"]
        token = base64.b64encode(f"{self.company}+{cfg['CW_PUBLIC_KEY']}:{cfg['CW_PRIVATE_KEY']}".encode()).decode()
        self.headers = {"Authorization": f"Basic {token}", "clientId": cfg["CW_CLIENT_ID"],
                        "Accept": "application/json"}
        self.board = cfg["CW_BOARD_ID"]
        self.default_company = cfg["CW_DEFAULT_COMPANY"]
        self.priorities = priority_ids(cfg["CW_PRIORITY_IDS"])

    async def _base(self, http: httpx.AsyncClient) -> str:
        key = f"{self.site}/{self.company}"
        if key not in self._codebases:
            codebase = "v4_6_release/"
            try:
                r = await http.get(f"https://{self.site}/login/companyinfo/{self.company}")
                if r.status_code == 200 and (r.json() or {}).get("Codebase"):
                    codebase = r.json()["Codebase"]
            except (httpx.HTTPError, ValueError):
                pass  # the default codebase works for ConnectWise cloud
            self._codebases[key] = codebase
        return f"https://{self.site}/{self._codebases[key]}apis/3.0"

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, headers=self.headers) as http:
                r = await http.request(method, await self._base(http) + path, **kwargs)
        except httpx.HTTPError as err:
            raise PSAError(f"Can't reach ConnectWise at {self.site}: {type(err).__name__}: {err}") from err
        if r.status_code == 401:
            raise PSAError("ConnectWise rejected the API keys (401). Check the company ID, public and private keys")
        if r.status_code == 403:
            raise PSAError(f"The API member isn't allowed to do this (403 on {path.split('?')[0]}). "
                           "Check its security role")
        if r.status_code >= 400:
            raise PSAError(f"ConnectWise {method} {path.split('?')[0]} failed ({r.status_code}): {_error_text(r)}")
        return r.json() if r.content else None

    def _contact(self, c: dict) -> Contact:
        company = c.get("company") or {}
        email = next((i.get("value", "") for i in c.get("communicationItems") or []
                      if (i.get("communicationType") or "").lower() == "email"), "")
        return Contact(id=str(c["id"]), name=f"{c.get('firstName', '')} {c.get('lastName', '')}".strip(),
                       company_id=str(company.get("id", "")), company_name=company.get("name", ""), email=email)

    async def _contacts(self, child_condition: str) -> list[dict]:
        return await self._request("GET", "/company/contacts", params={
            "conditions": "inactiveFlag=false", "childConditions": child_condition, "pageSize": 10,
        }) or []

    async def find_by_phone(self, number: str) -> list[Contact]:
        tail = digits(number)[-8:]
        if len(tail) < 8:
            return []
        found = await self._contacts(f'communicationItems/value like "%{tail}%"')
        return [self._contact(c) for c in found
                if any(same_phone(i.get("value", ""), number) for i in c.get("communicationItems") or []
                       if (i.get("communicationType") or "").lower() == "phone")]

    async def find_by_email(self, email: str) -> list[Contact]:
        email = clean_email(email)
        if not email:
            return []
        found = await self._contacts(f'communicationItems/value="{email}"')
        return [c for c in map(self._contact, found) if c.email.lower() == email]

    async def _company_ref(self, value: str) -> dict:
        if value.isdigit():
            return {"id": int(value)}
        safe = re.sub(r'["\\]', "", value)
        found = await self._request("GET", "/company/companies",
                                    params={"conditions": f'identifier="{safe}"', "fields": "id"})
        if not found:
            raise PSAError(f'No ConnectWise company with the identifier "{value}"')
        return {"id": found[0]["id"]}

    async def create_ticket(self, t: TicketRequest) -> str:
        company = t.contact.company_id if t.contact else t.company_id
        if not company and not self.default_company:
            raise PSAError("The caller isn't a known contact and no default company is set")
        body: dict[str, Any] = {
            "summary": t.summary[:100],
            "board": {"id": int(self.board)},
            "company": await self._company_ref(company or self.default_company),
            "initialDescription": t.description,
            "initialInternalAnalysis": t.internal_note[:15000],
            "contactName": t.caller_name[:62],
            "contactPhoneNumber": digits(t.callback_phone)[:20],
        }
        if t.contact:
            body["contact"] = {"id": int(t.contact.id)}
        if clean_email(t.email):
            body["contactEmailAddress"] = clean_email(t.email)
        if t.priority in self.priorities:
            body["priority"] = {"id": int(self.priorities[t.priority])}
        created = await self._request("POST", "/service/tickets", json=body)
        return str(created["id"])

    async def test(self, lookup_number: str = "") -> str:
        boards = await self._request("GET", "/service/boards",
                                     params={"conditions": "inactiveFlag=false", "fields": "id,name", "pageSize": 50})
        prios = await self._request("GET", "/service/priorities", params={"fields": "id,name", "pageSize": 50})
        lines = ["Connected to ConnectWise.",
                 "Boards: " + ", ".join(f"{b['name']} = {b['id']}" for b in boards or []) + ".",
                 "Priorities: " + ", ".join(f"{p['name']} = {p['id']}" for p in prios or []) + "."]
        if self.board and not any(str(b["id"]) == self.board for b in boards or []):
            lines.append(f"Board {self.board} wasn't found or is inactive.")
        if lookup_number:
            lines.append(_describe_lookup(lookup_number, await self.find_by_phone(lookup_number)))
        return " ".join(lines)


# --- HaloPSA -----------------------------------------------------------------------------


class HaloPSA(PSA):
    name = "HaloPSA"
    _tokens: dict[tuple[str, str], tuple[str, float]] = {}

    def __init__(self, cfg: dict[str, str]) -> None:
        self.url = cfg["HALO_URL"].rstrip("/")
        self.client_id = cfg["HALO_CLIENT_ID"]
        self.secret = cfg["HALO_CLIENT_SECRET"]
        self.tenant = cfg["HALO_TENANT"]
        self.ticket_type = cfg["HALO_TICKET_TYPE_ID"]
        self.default_client = cfg["HALO_DEFAULT_CLIENT_ID"]
        self.priorities = priority_ids(cfg["HALO_PRIORITY_IDS"])

    async def _token(self, http: httpx.AsyncClient) -> str:
        key = (self.url, self.client_id)
        cached = self._tokens.get(key)
        if cached and cached[1] > time.time() + 60:
            return cached[0]
        r = await http.post(f"{self.url}/auth/token", params={"tenant": self.tenant} if self.tenant else None,
                            data={"grant_type": "client_credentials", "client_id": self.client_id,
                                  "client_secret": self.secret, "scope": "all"})
        if r.status_code != 200:
            raise PSAError(f"HaloPSA sign-in failed ({r.status_code}): {_error_text(r)}. Check the client ID "
                           "and secret, and that the application uses Client ID and Secret (Services)")
        body = r.json()
        self._tokens[key] = (body["access_token"], time.time() + int(body.get("expires_in", 3600)))
        return body["access_token"]

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as http:
                headers = {"Authorization": f"Bearer {await self._token(http)}", "Accept": "application/json"}
                r = await http.request(method, f"{self.url}/api{path}", headers=headers, **kwargs)
        except httpx.HTTPError as err:
            raise PSAError(f"Can't reach HaloPSA at {self.url}: {type(err).__name__}: {err}") from err
        if r.status_code in (401, 403):
            raise PSAError(f"HaloPSA refused {path} ({r.status_code}). Check the application's permissions "
                           "(read:customers, edit:tickets) and the agent it logs in as")
        if r.status_code >= 400:
            raise PSAError(f"HaloPSA {method} {path} failed ({r.status_code}): {_error_text(r)}")
        return r.json() if r.content else None

    @staticmethod
    def _contact(u: dict) -> Contact:
        return Contact(id=str(u["id"]), name=u.get("name", ""), company_id=str(u.get("client_id", "")),
                       company_name=u.get("client_name", ""), email=u.get("emailaddress", "") or "",
                       site_id=str(u.get("site_id", "") or ""))

    async def _users(self, search: str, **extra) -> list[dict]:
        data = await self._request("GET", "/Users", params={"search": search, "count": 20, **extra})
        return (data or {}).get("users", []) if isinstance(data, dict) else data or []

    async def find_by_phone(self, number: str) -> list[Contact]:
        tail = digits(number)[-8:]
        if len(tail) < 8:
            return []
        phone_keys = ("phonenumber_preferred", "phonenumber", "mobilenumber", "mobilenumber2", "homenumber")
        found = await self._users(tail, search_phonenumbers="true")
        return [self._contact(u) for u in found
                if not u.get("inactive") and any(same_phone(str(u.get(k) or ""), number) for k in phone_keys)]

    async def find_by_email(self, email: str) -> list[Contact]:
        email = clean_email(email)
        if not email:
            return []
        found = await self._users(email)
        return [self._contact(u) for u in found
                if not u.get("inactive") and (u.get("emailaddress") or "").strip().lower() == email]

    async def create_ticket(self, t: TicketRequest) -> str:
        client = t.contact.company_id if t.contact else (t.company_id or self.default_client)
        if not client:
            raise PSAError("The caller isn't a known contact and no default client is set")
        details = html.escape(t.description).replace("\n", "<br>")
        body: dict[str, Any] = {
            "summary": t.summary[:250],
            "details": details,
            "client_id": int(client),
            "reportedby": t.caller_name,
        }
        if self.ticket_type:
            body["tickettype_id"] = int(self.ticket_type)
        if t.contact:
            body["user_id"] = int(t.contact.id)
            if t.contact.site_id:
                body["site_id"] = int(t.contact.site_id)
        if t.priority in self.priorities:
            body["priority_id"] = int(self.priorities[t.priority])
        created = await self._request("POST", "/Tickets", json=[body])
        ticket = created[0] if isinstance(created, list) else created
        ticket_id = str(ticket["id"])
        try:  # the transcript goes in a private note; a failure here mustn't lose the ticket number
            await self._request("POST", "/Actions", json=[{
                "ticket_id": int(ticket_id), "outcome": "Private Note", "hiddenfromuser": True,
                "note_html": html.escape(t.internal_note).replace("\n", "<br>"),
            }])
        except PSAError as err:
            logger.warning(f"HaloPSA ticket {ticket_id} created, but adding the call note failed: {err}")
        return ticket_id

    async def test(self, lookup_number: str = "") -> str:
        types = await self._request("GET", "/TicketType", params={"showinactive": "false"})
        prios = await self._request("GET", "/Priority")
        lines = ["Connected to HaloPSA.",
                 "Ticket types: " + ", ".join(f"{t.get('name')} = {t.get('id')}" for t in _items(types)[:30]) + ".",
                 "Priorities: " + ", ".join(f"{p.get('name')} = {p.get('priorityid', p.get('id'))}"
                                            for p in _items(prios)[:30]) + "."]
        if lookup_number:
            lines.append(_describe_lookup(lookup_number, await self.find_by_phone(lookup_number)))
        return " ".join(lines)


def _items(data: Any) -> list[dict]:
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                return [d for d in v if isinstance(d, dict)]
    return []


def _describe_lookup(number: str, found: list[Contact]) -> str:
    if not found:
        return f"No contact found for {number}."
    return f"{number} matches: " + "; ".join(f"{c.name} ({c.company_name})" for c in found[:5]) + "."


# --- entry points ------------------------------------------------------------------------

REQUIRED = {
    "connectwise": ("CW_SITE", "CW_COMPANY_ID", "CW_PUBLIC_KEY", "CW_PRIVATE_KEY", "CW_CLIENT_ID", "CW_BOARD_ID"),
    "halopsa": ("HALO_URL", "HALO_CLIENT_ID", "HALO_CLIENT_SECRET"),
}


def missing(cfg: dict[str, str]) -> list[str]:
    """Settings the selected PSA still needs (keys), or [] if it's ready or none is selected."""
    return [k for k in REQUIRED.get(cfg.get("PSA_PROVIDER", "none"), ()) if not cfg.get(k)]


def get(cfg: dict[str, str]) -> PSA | None:
    provider = cfg.get("PSA_PROVIDER", "none")
    if provider == "none" or missing(cfg):
        return None
    return ConnectWise(cfg) if provider == "connectwise" else HaloPSA(cfg)


async def lookup_caller(psa: PSA, number: str, timeout: float = 3.0) -> Contact | list[Contact] | None:
    """The contact whose phone number matches the caller ID: one Contact, several, or None.
    Never raises: a slow or failing PSA mustn't hold up answering the call."""
    if not number:
        return None
    try:
        found = await asyncio.wait_for(psa.find_by_phone(number), timeout)
    except Exception as err:
        logger.warning(f"{psa.name} caller ID lookup failed: {err!r}")
        return None
    unique = list({c.id: c for c in found}.values())
    if not unique:
        return None
    return unique[0] if len(unique) == 1 else unique


def _name_tokens(name: str) -> list[str]:
    return re.findall(r"[a-z]+", (name or "").lower())


def names_match(contact_name: str, caller_name: str) -> bool:
    """The caller gave the contact's first name, or their full name."""
    a, b = _name_tokens(contact_name), _name_tokens(caller_name)
    return bool(a and b) and (a[0] == b[0] or a == b)


async def resolve_contact(
    psa: PSA, caller_match: Contact | list[Contact] | None, caller_name: str, email: str
) -> tuple[Contact | None, str]:
    """Which contact to link the ticket to, and its company ID.

    A caller ID match only counts if the caller gave that contact's name: someone else may be
    using their phone. Otherwise the email they gave is looked up. If neither identifies them but
    every caller ID match is at the same company, the ticket still goes to that company."""
    matches = [caller_match] if isinstance(caller_match, Contact) else list(caller_match or [])
    for c in matches:
        if names_match(c.name, caller_name):
            return c, c.company_id
    if clean_email(email):
        try:
            found = list({c.id: c for c in await asyncio.wait_for(psa.find_by_email(email), 5)}.values())
            if len(found) == 1:
                return found[0], found[0].company_id
        except Exception as err:
            logger.warning(f"{psa.name} email lookup failed: {err!r}")
    companies = {c.company_id for c in matches if c.company_id}
    if len(companies) == 1:
        return None, companies.pop()
    return None, ""


def caller_hint(match: Contact | list[Contact] | None) -> str:
    """What the agent is told about a caller ID match."""
    if isinstance(match, Contact):
        email = f", email {match.email}" if match.email else ""
        return (
            f"\n\nThe caller ID matches a contact in the helpdesk system: {match.name} from "
            f"{match.company_name}{email}. Someone else may be using their phone, so ask for the caller's "
            "name as usual and don't use the contact's name until they've said it. If the name they give "
            "matches, confirm their organisation and email address with them instead of asking for them "
            "from scratch. If it doesn't, collect their details as normal."
        )
    companies = {c.company_name for c in match or [] if c.company_name}
    if len(companies) == 1:
        return (
            f"\n\nThe caller ID matches several contacts at {companies.pop()} in the helpdesk system. Collect "
            "the caller's details as normal, but you can confirm their organisation instead of asking for it."
        )
    return ""
