"""Caller verification and password resets: Microsoft Entra ID (Graph) and SMTP2GO SMS.

Every security decision is made here, in code, not by the AI model:
- The model can look up an account, but never sees the employee ID or phone number on it.
- Verification is by employee ID if the account has one, otherwise by a code sent by SMS to the
  phone number on the account. The model can't choose the method or the number.
- A reset only works for the account that was verified on this call, and only once.
- Attempts are limited per call and per account (across calls), and every attempt is audited.

Entra app registration (Application permissions, admin consent granted):
- User.Read.All                         look up accounts (employee ID, mobile phone)
- User-PasswordProfile.ReadWrite.All    reset passwords
- RoleManagement.Read.Directory         refuse resets for accounts holding admin roles
- GroupMember.Read.All                  only if "Only allow resets for group" is set
- UserAuthenticationMethod.Read.All     only if SMS numbers come from MFA phone methods
and the app's service principal assigned the Helpdesk Administrator (or User Administrator) role.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

GRAPH = "https://graph.microsoft.com/v1.0"
SMTP2GO_SMS_URL = "https://api.smtp2go.com/v3/sms/send"

MAX_ID_ATTEMPTS = 3  # employee ID guesses per call
MAX_CODE_ATTEMPTS = 3  # SMS code guesses per code
MAX_SMS_SENDS = 2  # codes sent per call
CODE_TTL = 600  # seconds an SMS code is valid
# Across calls, per account: too many failed verifications locks phone resets for a while.
MAX_FAILURES_PER_WINDOW = 5
FAILURE_WINDOW = 3600
MAX_RESETS_PER_DAY = 3

AUDIT_FILE = Path("/srv/tickets/password-resets.log")


class IdentityError(Exception):
    """Something went wrong talking to Entra or SMTP2GO. The message is safe to log."""


# --- Microsoft Graph ---------------------------------------------------------------------


class GraphClient:
    _tokens: dict[tuple[str, str], tuple[str, float]] = {}  # (tenant, client) -> (token, expiry)

    def __init__(self, cfg: dict[str, str]) -> None:
        self._tenant = cfg["ENTRA_TENANT_ID"]
        self._client = cfg["ENTRA_CLIENT_ID"]
        self._secret = cfg["ENTRA_CLIENT_SECRET"]

    async def _token(self, http: httpx.AsyncClient) -> str:
        key = (self._tenant, self._client)
        cached = self._tokens.get(key)
        if cached and cached[1] > time.time() + 60:
            return cached[0]
        r = await http.post(
            f"https://login.microsoftonline.com/{self._tenant}/oauth2/v2.0/token",
            data={"grant_type": "client_credentials", "client_id": self._client,
                  "client_secret": self._secret, "scope": "https://graph.microsoft.com/.default"},
        )
        if r.status_code != 200:
            raise IdentityError(f"Entra sign-in failed ({r.status_code}): {_graph_error(r)}")
        body = r.json()
        self._tokens[key] = (body["access_token"], time.time() + int(body.get("expires_in", 3600)))
        return body["access_token"]

    async def request(self, method: str, path: str, *, ok=(200,), **kwargs) -> httpx.Response:
        extra_headers = kwargs.pop("headers", {})
        for attempt in (1, 2):
            try:
                async with httpx.AsyncClient(timeout=15) as http:
                    headers = {"Authorization": f"Bearer {await self._token(http)}", **extra_headers}
                    r = await http.request(method, GRAPH + path, headers=headers, **kwargs)
                break
            except httpx.TransportError as err:
                # Dropped connections/timeouts happen; every Graph call we make is idempotent, so retry once.
                if attempt == 2:
                    raise IdentityError(f"Graph {method} {path.split('?')[0]} unreachable: "
                                        f"{type(err).__name__}: {err}") from err
                logger.warning(f"Graph {method} {path.split('?')[0]} transport error, retrying: {err!r}")
                await asyncio.sleep(0.5)
        if r.status_code not in ok:
            raise IdentityError(f"Graph {method} {path.split('?')[0]} failed ({r.status_code}): {_graph_error(r)}")
        return r

    async def get(self, path: str, **kwargs) -> dict[str, Any]:
        return (await self.request("GET", path, **kwargs)).json()


def _graph_error(r: httpx.Response) -> str:
    try:
        body = r.json()
        err = body.get("error")
        if isinstance(err, dict):
            return f"{err.get('code', '')}: {err.get('message', '')}"[:300]
        return str(body.get("error_description") or err)[:300]
    except ValueError:
        return r.text[:300]


USER_FIELDS = ("id,displayName,userPrincipalName,mail,employeeId,mobilePhone,accountEnabled,"
               "onPremisesSyncEnabled,userType,department")


def _odata_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


async def find_users(graph: GraphClient, query: str) -> list[dict[str, Any]]:
    """Accounts matching an email address/UPN exactly, or a display name."""
    query = query.strip()
    if "@" in query:
        q = _odata_str(query.lower())
        flt = f"userPrincipalName eq {q} or mail eq {q}"
        data = await graph.get(f"/users?$filter={flt}&$select={USER_FIELDS}&$top=5")
    else:
        name = re.sub(r'["\\]', "", query)
        data = await graph.get(
            f'/users?$search="displayName:{name}"&$select={USER_FIELDS}&$top=5',
            headers={"ConsistencyLevel": "eventual"},
        )
    return [u for u in data.get("value", []) if u.get("userType", "Member") == "Member"]


async def has_admin_role(graph: GraphClient, user_id: str) -> bool:
    data = await graph.get(f"/users/{user_id}/transitiveMemberOf/microsoft.graph.directoryRole?$select=id,displayName")
    return bool(data.get("value"))


async def in_group(graph: GraphClient, user_id: str, group_id: str) -> bool:
    r = await graph.request("POST", f"/users/{user_id}/checkMemberGroups", json={"groupIds": [group_id]})
    return group_id in r.json().get("value", [])


async def mfa_mobile(graph: GraphClient, user_id: str) -> str:
    data = await graph.get(f"/users/{user_id}/authentication/phoneMethods")
    for m in data.get("value", []):
        if m.get("phoneType") == "mobile" and m.get("phoneNumber"):
            return m["phoneNumber"]
    return ""


async def set_password(graph: GraphClient, user_id: str, password: str) -> None:
    await graph.request(
        "PATCH", f"/users/{user_id}", ok=(204,),
        json={"passwordProfile": {"password": password, "forceChangePasswordNextSignIn": True}},
    )


async def test_connection(cfg: dict[str, str]) -> str:
    """Checks the credentials and permissions the agent needs. Returns a summary."""
    graph = GraphClient(cfg)

    async def step(what: str, permission: str, check):
        try:
            return await check
        except IdentityError as err:
            if "(403)" in str(err):
                raise IdentityError(f"Signed in, but can't {what}. Add the Microsoft Graph application "
                                    f"permission {permission} and grant admin consent.") from err
            raise

    data = await step("read users", "User.Read.All", graph.get("/users?$top=1&$select=id,employeeId,mobilePhone"))
    users = data.get("value", [])
    if not users:
        return "Connected to Entra, but no users were found."
    uid = users[0]["id"]
    await step("read admin roles", "RoleManagement.Read.Directory", has_admin_role(graph, uid))
    checks = ["read users", "read admin roles"]
    if cfg["RESET_ALLOWED_GROUP_ID"]:
        await step("check group membership", "GroupMember.Read.All", in_group(graph, uid, cfg["RESET_ALLOWED_GROUP_ID"]))
        checks.append("check group membership")
    if cfg["SMS_PHONE_SOURCE"] in ("mfa", "either"):
        await step("read MFA phone numbers (needed because SMS codes go to MFA numbers)",
                   "UserAuthenticationMethod.Read.All", mfa_mobile(graph, uid))
        checks.append("read MFA phone numbers")
    return ("Connected to Entra. Permissions OK to " + ", ".join(checks) + ". Password resets also need "
            "User-PasswordProfile.ReadWrite.All and the Helpdesk Administrator role, which can only be "
            "confirmed by a real reset.")


# --- SMTP2GO SMS -------------------------------------------------------------------------


def to_e164(number: str, country_code: str) -> str:
    """'0412 345 678' -> '+61412345678'. Returns '' if it doesn't look like a phone number."""
    n = re.sub(r"[\s().-]", "", number or "")
    if n.startswith("0011"):  # Australian international prefix
        n = "+" + n[4:]
    elif n.startswith("00"):
        n = "+" + n[2:]
    elif n.startswith("0"):
        n = f"+{country_code}{n[1:]}"
    elif not n.startswith("+"):
        n = f"+{country_code}{n}" if not n.startswith(country_code) else "+" + n
    return n if re.fullmatch(r"\+[1-9][0-9]{7,14}", n) else ""


async def send_sms(cfg: dict[str, str], number: str, text: str) -> None:
    body: dict[str, Any] = {"destination": [number], "content": text}
    if cfg["SMS_SENDER"]:
        body["sender"] = cfg["SMS_SENDER"]
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            r = await http.post(SMTP2GO_SMS_URL, json=body, headers={"X-Smtp2go-Api-Key": cfg["SMTP2GO_API_KEY"]})
    except httpx.HTTPError as err:  # no retry: the first attempt may have sent the text
        raise IdentityError(f"SMTP2GO SMS unreachable: {type(err).__name__}: {err}") from err
    try:
        data = r.json().get("data", {})
    except ValueError:
        data = {}
    if r.status_code != 200 or not data.get("total_sent"):
        raise IdentityError(f"SMTP2GO SMS failed ({r.status_code}): {data.get('error') or r.text[:200]}")


# --- Per-account limits (in memory, across calls) ----------------------------------------

_failures: dict[str, list[float]] = {}
_resets: dict[str, list[float]] = {}


def _recent(log: dict[str, list[float]], key: str, window: float) -> list[float]:
    now = time.time()
    log[key] = [t for t in log.get(key, []) if now - t < window]
    return log[key]


def locked_out(user_id: str) -> bool:
    return len(_recent(_failures, user_id, FAILURE_WINDOW)) >= MAX_FAILURES_PER_WINDOW


def record_failure(user_id: str) -> None:
    _recent(_failures, user_id, FAILURE_WINDOW).append(time.time())


def too_many_resets(user_id: str) -> bool:
    return len(_recent(_resets, user_id, 86400)) >= MAX_RESETS_PER_DAY


def record_reset(user_id: str) -> None:
    _recent(_resets, user_id, 86400).append(time.time())
    _failures.pop(user_id, None)


def audit(event: str, **details: Any) -> None:
    """Append-only log of every verification and reset attempt. Never contains secrets."""
    record = {"time": datetime.now(timezone.utc).isoformat(), "event": event, **details}
    logger.info(f"Password reset audit: {record}")
    try:
        with AUDIT_FILE.open("a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        logger.exception("Couldn't write the password reset audit log")


# --- Temporary passwords -----------------------------------------------------------------

# Easy to say and hear on a phone line: no homophones, no letters that sound alike.
WORDS = (
    "Apple Arrow Autumn Badger Bamboo Banjo Basket Beacon Biscuit Blossom Bridge Bucket Cactus Camel "
    "Canyon Carpet Castle Cherry Cobalt Comet Copper Coral Cricket Dolphin Dragon Ember Falcon Feather "
    "Forest Garden Ginger Glacier Granite Harbour Hazel Helmet Island Jacket Jungle Kettle Koala Ladder "
    "Lantern Lemon Lizard Magnet Mango Maple Meadow Melon Mirror Monkey Muffin Nectar Nugget Ocean Olive "
    "Orange Orbit Otter Paddle Panda Parrot Pebble Pepper Pickle Pillow Planet Pocket Puzzle Rabbit "
    "Rainbow Ribbon River Rocket Saddle Salmon Silver Spider Sunset Tablet Tiger Timber Tomato Tractor "
    "Tunnel Turtle Velvet Violin Walnut Wombat Yellow Zebra"
).split()


def temporary_password() -> str:
    a, b = secrets.choice(WORDS), secrets.choice(WORDS)
    while b == a:
        b = secrets.choice(WORDS)
    return f"{a}-{b}-{secrets.randbelow(9000) + 1000}"


def spoken_password(password: str) -> str:
    a, b, digits = password.split("-")
    return (f"{a}, with a capital {a[0]}, then a dash, then {b}, with a capital {b[0]}, then a dash, "
            f"then the numbers {', '.join(digits)}.")


# --- One call's verification state -------------------------------------------------------


def _norm(value: str) -> str:
    return re.sub(r"[^0-9a-z]", "", (value or "").lower())


@dataclass
class ResetSession:
    """What's been looked up and verified on this call. The model only ever sees tool results."""

    user: dict[str, Any] | None = None
    method: str = ""  # "employee_id" | "sms" | "" (can't verify)
    sms_number: str = ""
    verified: bool = False
    id_attempts: int = 0
    code: str = ""
    code_expires: float = 0
    code_attempts: int = 0
    sms_sends: int = 0
    reset_done: bool = False
    temp_password: str = ""
    repeats: int = 0
    blocked: bool = False  # verification failed too often: no more resets on this call
    redact: list[str] = field(default_factory=list)  # secrets to strip from ticket transcripts

    def select(self, user: dict[str, Any], method: str, sms_number: str) -> None:
        # Switching accounts always starts verification again.
        self.user, self.method, self.sms_number = user, method, sms_number
        self.verified = False
        self.id_attempts = self.code_attempts = 0
        self.code, self.code_expires = "", 0
        if user.get("employeeId"):
            self.redact.append(user["employeeId"])

    def check_employee_id(self, spoken: str) -> bool:
        self.id_attempts += 1
        expected = _norm(self.user["employeeId"]) if self.user else ""
        return bool(expected) and hmac.compare_digest(_norm(spoken), expected)

    def new_code(self) -> str:
        self.code = f"{secrets.randbelow(1_000_000):06d}"
        self.code_expires = time.time() + CODE_TTL
        self.code_attempts = 0
        self.sms_sends += 1
        self.redact.append(self.code)
        return self.code

    def check_code(self, spoken: str) -> bool:
        self.code_attempts += 1
        return bool(self.code) and time.time() < self.code_expires and hmac.compare_digest(
            re.sub(r"\D", "", spoken or ""), self.code)


def redact_transcript(messages: list[dict[str, Any]], secrets_: list[str]) -> list[dict[str, Any]]:
    """Removes employee IDs and SMS codes (however they were spaced when spoken) from a transcript."""
    if not secrets_:
        return messages
    patterns = [re.compile(r"[\s,./-]*".join(re.escape(c) for c in s), re.IGNORECASE)
                for s in {_norm(s) for s in secrets_} if len(s) >= 3]
    text = json.dumps(messages, default=str)
    for p in patterns:
        text = p.sub("[redacted]", text)
    try:
        return json.loads(text)
    except ValueError:
        return [{"role": "system", "content": "[transcript removed: contained verification details]"}]
