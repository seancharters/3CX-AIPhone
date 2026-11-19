"""Admin web UI: API keys, 3CX extension details and agent options.

Runs as its own container (same image as the agent) and is only published on the server's
localhost. Reach it with an SSH tunnel: ssh -L 8000:localhost:8000 you@server
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import re
import secrets
from datetime import datetime
from itertools import groupby

from fastapi import FastAPI, Form, Request, UploadFile
import httpx
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from loguru import logger
from starlette.middleware.sessions import SessionMiddleware

from . import identity, psa, settings
from .asterisk import AMIClient
from .triage import tickets

ADMIN_FILE = settings.DATA_DIR / "admin.json"
SESSION_SECRET_FILE = settings.DATA_DIR / "session_secret"
MIN_PASSWORD = 10
BACKUP_FORMAT = "it-triage-agent-settings"


def _session_secret() -> str:
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not SESSION_SECRET_FILE.exists():
        settings._write_private(SESSION_SECRET_FILE, secrets.token_hex(32))
    return SESSION_SECRET_FILE.read_text().strip()


def _hash(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1).hex()


def _password_set() -> bool:
    return ADMIN_FILE.exists()


def _check_password(password: str) -> bool:
    data = json.loads(ADMIN_FILE.read_text())
    return hmac.compare_digest(_hash(password, bytes.fromhex(data["salt"])), data["hash"])


def _set_password(password: str) -> None:
    salt = secrets.token_bytes(16)
    settings._write_private(ADMIN_FILE, json.dumps({"salt": salt.hex(), "hash": _hash(password, salt)}))


# Until an admin password exists, the first visitor is asked to create one. That's safe because
# the admin port is only published on the server's loopback (see docker-compose.yml), so only
# someone already on the server, or tunnelled in over SSH, can reach it. Don't expose the port
# publicly before the password is set.
_setup_lock = asyncio.Lock()  # two simultaneous first visits can't both set the password

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=_session_secret(), same_site="strict", max_age=8 * 3600)
ami = AMIClient(host="asterisk", port=5038, username="agent", secret=settings.ami_secret())


# --- pages -------------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not _password_set():
        return RedirectResponse("/setup", status_code=303)
    if not request.session.get("admin"):
        return RedirectResponse("/login", status_code=303)
    return _page("Settings", _settings_form(settings.load(), request.session.pop("flash", None)), nav=True)


def _apply_form(form) -> tuple[dict[str, str], dict[str, str], str]:
    """Save the settings form. Returns (previous, new, error)."""
    current = settings.load()
    updates: dict[str, str] = {}
    for f in settings.FIELDS:
        value = str(form.get(f.key, ""))
        if f.secret:
            if form.get(f"clear_{f.key}"):
                value = ""
            elif not value:
                continue  # blank secret field = keep the stored value
        updates[f.key] = value
    try:
        return current, settings.save(updates), ""
    except settings.ValidationError as e:
        merged = current | {k: v for k, v in updates.items() if not settings.FIELDS_BY_KEY[k].secret}
        return current, merged, str(e)


@app.post("/", response_class=HTMLResponse)
async def save(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse("/login", status_code=303)
    current, new, error = _apply_form(await request.form())
    if error:
        return _page("Settings", _settings_form(new, ("error", error)), nav=True)
    logger.info("Settings updated from admin UI")
    request.session["flash"] = (
        "ok",
        "Saved. New settings apply to the next call. Asterisk re-registers with 3CX within a few seconds."
        if new != current
        else "No changes.",
    )
    return RedirectResponse("/", status_code=303)


@app.post("/test-email", response_class=HTMLResponse)
async def test_email(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse("/login", status_code=303)
    _, cfg, error = _apply_form(await request.form())
    if error:
        return _page("Settings", _settings_form(cfg, ("error", error)), nav=True)
    missing = [settings.FIELDS_BY_KEY[k].label for k in ("EMAIL_TO", "EMAIL_FROM", "SMTP_HOST") if not cfg[k]]
    if missing:
        request.session["flash"] = ("error", f"Saved, but can't send a test yet. Fill in: {', '.join(missing)}.")
        return RedirectResponse("/#tickets", status_code=303)
    try:
        await asyncio.to_thread(tickets.send_email, cfg, tickets.test_email(cfg))
        request.session["flash"] = ("ok", f"Saved, and a test email was sent to {cfg['EMAIL_TO']}. Check the inbox (and junk).")
    except Exception as e:
        logger.warning(f"Test email failed: {e!r}")
        request.session["flash"] = ("error", f"Saved, but the test email failed: {type(e).__name__}: {e}")
    return RedirectResponse("/#tickets", status_code=303)


@app.post("/test-entra", response_class=HTMLResponse)
async def test_entra(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse("/login", status_code=303)
    _, cfg, error = _apply_form(await request.form())
    if error:
        return _page("Settings", _settings_form(cfg, ("error", error)), nav=True)
    missing = [settings.FIELDS_BY_KEY[k].label for k in ("ENTRA_TENANT_ID", "ENTRA_CLIENT_ID", "ENTRA_CLIENT_SECRET")
               if not cfg[k]]
    if missing:
        request.session["flash"] = ("error", f"Saved, but can't test Entra yet. Fill in: {', '.join(missing)}.")
        return RedirectResponse("/#resets", status_code=303)
    try:
        request.session["flash"] = ("ok", "Saved. " + await identity.test_connection(cfg))
    except identity.IdentityError as err:
        request.session["flash"] = ("error", f"Saved, but the Entra test failed: {err}")
    except Exception as err:
        logger.warning(f"Entra test failed: {err!r}")
        request.session["flash"] = ("error", f"Saved, but the Entra test failed: {type(err).__name__}: {err}")
    return RedirectResponse("/#resets", status_code=303)


@app.post("/test-sms", response_class=HTMLResponse)
async def test_sms(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    _, cfg, error = _apply_form(form)
    if error:
        return _page("Settings", _settings_form(cfg, ("error", error)), nav=True)
    number = identity.to_e164(str(form.get("test_sms_to", "")), cfg["SMS_COUNTRY_CODE"])
    if not cfg["SMTP2GO_API_KEY"] or not number:
        request.session["flash"] = ("error", "Saved, but can't send a test SMS: enter the SMTP2GO API key and a "
                                    "mobile number to send it to.")
        return RedirectResponse("/#resets", status_code=303)
    try:
        await identity.send_sms(cfg, number, "Test message from the IT support phone agent. SMS verification "
                                "codes will arrive like this.")
        request.session["flash"] = ("ok", f"Saved, and a test SMS was sent to {number}.")
    except Exception as err:
        logger.warning(f"Test SMS failed: {err!r}")
        request.session["flash"] = ("error", f"Saved, but the test SMS failed: {err}")
    return RedirectResponse("/#resets", status_code=303)


@app.post("/test-psa", response_class=HTMLResponse)
async def test_psa(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    _, cfg, error = _apply_form(form)
    if error:
        return _page("Settings", _settings_form(cfg, ("error", error)), nav=True)
    if cfg["PSA_PROVIDER"] == "none":
        request.session["flash"] = ("error", "Saved. Choose ConnectWise or HaloPSA to test the connection.")
        return RedirectResponse("/#tickets", status_code=303)
    if missing := psa.missing(cfg):
        labels = ", ".join(settings.FIELDS_BY_KEY[k].label for k in missing)
        request.session["flash"] = ("error", f"Saved, but can't test yet. Fill in: {labels}.")
        return RedirectResponse("/#tickets", status_code=303)
    helpdesk = psa.get(cfg)
    try:
        result = await helpdesk.test(str(form.get("test_psa_number", "")).strip())
        request.session["flash"] = ("ok", "Saved. " + result)
    except psa.PSAError as err:
        request.session["flash"] = ("error", f"Saved, but the {helpdesk.name} test failed: {err}")
    except Exception as err:
        logger.warning(f"PSA test failed: {err!r}")
        request.session["flash"] = ("error", f"Saved, but the {helpdesk.name} test failed: {type(err).__name__}: {err}")
    return RedirectResponse("/#tickets", status_code=303)


@app.get("/backup")
async def backup(request: Request):
    """All settings, API keys included, as a JSON file to download."""
    if not request.session.get("admin"):
        return RedirectResponse("/login", status_code=303)
    data = {"format": BACKUP_FORMAT, "version": 1, "created": datetime.now().isoformat(timespec="seconds"),
            "settings": settings.stored()}
    name = f"it-triage-agent-backup-{datetime.now():%Y%m%d-%H%M}.json"
    logger.info("Settings backup downloaded from admin UI")
    return Response(
        json.dumps(data, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"},
    )


@app.post("/restore")
async def restore(request: Request, file: UploadFile):
    """Replace all settings with those in a backup file."""
    if not request.session.get("admin"):
        return RedirectResponse("/login", status_code=303)
    try:
        data = json.loads(await file.read(1_000_000))
        if not isinstance(data, dict) or data.get("format") != BACKUP_FORMAT:
            raise ValueError
        backed_up = data["settings"]
        if not isinstance(backed_up, dict):
            raise ValueError
    except (ValueError, KeyError, UnicodeDecodeError):
        request.session["flash"] = ("error", "That isn't a backup from this admin page. Nothing was changed.")
        return RedirectResponse("/#backup", status_code=303)
    try:
        # Anything missing from the backup goes back to its default.
        settings.save({f.key: str(backed_up.get(f.key) or "") for f in settings.FIELDS})
    except settings.ValidationError as err:
        request.session["flash"] = ("error", f"Couldn't restore, nothing was changed. {err}")
        return RedirectResponse("/#backup", status_code=303)
    ignored = sorted(set(backed_up) - set(settings.FIELDS_BY_KEY))
    logger.info(f"Settings restored from backup made {data.get('created', 'at an unknown time')}")
    request.session["flash"] = (
        "ok",
        f"Restored the backup from {data.get('created', 'an unknown date')}. New settings apply to the next "
        "call, and Asterisk re-registers with 3CX within a few seconds."
        + (f" Ignored settings this version doesn't have: {', '.join(ignored)}." if ignored else ""),
    )
    return RedirectResponse("/#backup", status_code=303)


@app.get("/status")
async def status(request: Request):
    if not request.session.get("admin"):
        return JSONResponse({"error": "unauthorised"}, status_code=401)
    cfg = settings.load()
    if not cfg["THREECX_HOST"]:
        return {"state": "unconfigured", "detail": "Enter the 3CX extension details below."}
    try:
        output = await ami.command("pjsip show registrations")
    except Exception as e:
        return {"state": "error", "detail": f"Can't reach Asterisk: {e}"}
    match = re.search(r"threecx-reg/\S+\s+\S+\s+(\w+)", output)
    state = match.group(1) if match else "Unknown"
    detail = {
        "Registered": f"Extension {cfg['THREECX_EXTENSION']} is online in 3CX.",
        "Rejected": "3CX rejected the login. Check the Auth ID and password, and that the extension "
        "may be used outside the LAN.",
        "Unregistered": "Not registered yet. If this persists, check the 3CX FQDN and firewall.",
    }.get(state, "")
    return {"state": state.lower(), "detail": detail}


@app.get("/live", response_class=HTMLResponse)
async def live_page(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse("/login", status_code=303)
    return _page("Live calls", _LIVE_BODY, nav=True, wide=True)


@app.get("/live/stream")
async def live_stream(request: Request):
    """Relays the agent's internal call events to a signed-in browser."""
    if not request.session.get("admin"):
        return JSONResponse({"error": "unauthorised"}, status_code=401)

    async def relay():
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=None)) as client:
                async with client.stream("GET", "http://agent:8080/events") as upstream:
                    async for chunk in upstream.aiter_raw():
                        if await request.is_disconnected():
                            break
                        yield chunk
        except httpx.HTTPError as err:
            yield f"data: {json.dumps({'type': 'error', 'text': f'Can’t reach the agent: {err}'})}\n\n".encode()

    # X-Accel-Buffering stops nginx-based reverse proxies (e.g. NGINX Proxy Manager) holding the stream
    # in their buffer; no-transform stops proxies compressing it, which also delays events.
    headers = {"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"}
    return StreamingResponse(relay(), media_type="text/event-stream", headers=headers)


@app.get("/setup", response_class=HTMLResponse)
async def setup_page():
    if _password_set():
        return RedirectResponse("/login", status_code=303)
    return _page("First-time setup", _setup_form())


@app.post("/setup", response_class=HTMLResponse)
async def setup(request: Request, password: str = Form(""), confirm: str = Form("")):
    if len(password) < MIN_PASSWORD or password != confirm:
        return _page("First-time setup", _setup_form(f"Passwords must match and be at least {MIN_PASSWORD} characters."))
    async with _setup_lock:
        if _password_set():
            return RedirectResponse("/login", status_code=303)
        _set_password(password)
    request.session["admin"] = True
    return RedirectResponse("/", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return _page("Sign in", _login_form())


@app.post("/login", response_class=HTMLResponse)
async def login(request: Request, password: str = Form("")):
    if _password_set() and _check_password(password):
        request.session["admin"] = True
        return RedirectResponse("/", status_code=303)
    await asyncio.sleep(1)
    return _page("Sign in", _login_form("Wrong password."))


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --- HTML --------------------------------------------------------------------------------

e = html.escape


# Settings are grouped into tabs. Every section in settings.FIELDS must be on exactly one tab.
TABS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("general", "General", ("Agent",)),
    ("voice", "Voice and AI", ("Providers", "Azure", "Anthropic", "Deepgram", "ElevenLabs")),
    ("prompt", "System prompt", ("System prompt",)),
    ("phone", "Phone system", ("3CX extension", "Server")),
    ("tickets", "Tickets", ("Helpdesk (PSA)", "Dropped calls", "Email tickets")),
    ("resets", "Password resets", ("Password resets",)),
    ("backup", "Backup", ()),
)
assert sorted(s for _, _, ss in TABS for s in ss) == sorted({f.section for f in settings.FIELDS})


def _show_if_attr(key: str, values: dict[str, str], allowed: tuple[str, ...]) -> str:
    rule = e(json.dumps({key: list(allowed)}))
    return f" data-show-if='{rule}'" + ("" if values.get(key) in allowed else " hidden")


def _section_extras(section: str, values: dict[str, str]) -> str:
    """Test buttons and other extras at the bottom of a section."""
    if section == "Email tickets":
        return (f'<div class="actions"{_show_if_attr("TICKET_EMAIL_ENABLED", values, ("yes",))}>'
                '<button type="submit" formaction="/test-email" class="secondary">Save and send test email</button></div>')
    if section == "Helpdesk (PSA)":
        on = ("connectwise", "halopsa")
        return (
            f'<div class="field"{_show_if_attr("PSA_PROVIDER", values, on)}><label for="test_psa_number">'
            'Look up a test number</label><input id="test_psa_number" name="test_psa_number" '
            'placeholder="Optional, e.g. 0412 345 678"><small>Not saved. The test shows which contact this '
            "number matches, the way a caller ID would be matched</small></div>"
            f'<div class="actions"{_show_if_attr("PSA_PROVIDER", values, on)}>'
            '<button type="submit" formaction="/test-psa" class="secondary">Save and test connection</button></div>'
        )
    if section == "Password resets":
        on = ("yes",)
        return (
            f'<div class="field"{_show_if_attr("PASSWORD_RESET_ENABLED", values, on)}>'
            '<label for="test_sms_to">Send a test SMS to</label>'
            '<input id="test_sms_to" name="test_sms_to" placeholder="e.g. 0412 345 678">'
            "<small>Not saved. Only used by the test SMS button</small></div>"
            f'<div class="actions gap"{_show_if_attr("PASSWORD_RESET_ENABLED", values, on)}>'
            '<button type="submit" formaction="/test-sms" class="secondary">Save and send test SMS</button>'
            '<button type="submit" formaction="/test-entra" class="secondary">Save and test Entra</button></div>'
        )
    return ""


def _error_tab(message: str) -> str:
    """The tab holding the first setting named in a validation error, so it opens there."""
    for tab_id, _, sections in TABS:
        for f in settings.FIELDS:
            if f.section in sections and f"{f.label}:" in message:
                return tab_id
    return ""


def _settings_form(values: dict[str, str], flash: tuple[str, str] | None) -> str:
    by_section: dict[str, list[settings.Field]] = {}
    for f in settings.FIELDS:
        by_section.setdefault(f.section, []).append(f)

    nav = "".join(f'<a href="#{tid}" data-tab="{tid}">{e(label)}</a>' for tid, label, _ in TABS)
    parts = [
        '<div class="layout"><aside class="sidenav">'
        '<div class="status"><span id="dot" class="dot"></span><div><strong id="state">Checking…</strong>'
        '<small id="detail"></small></div></div>'
        f'<nav class="tabs">{nav}</nav></aside><div class="content">'
    ]
    if flash:
        error_tab = _error_tab(flash[1]) if flash[0] == "error" else ""
        parts.append(f'<p class="flash {e(flash[0])}" data-tab-hint="{error_tab}">{e(flash[1])}</p>')
    parts.append('<form method="post" autocomplete="off" id="settings">')
    for tab_id, label, sections in TABS:
        if not sections:
            continue
        parts.append(f'<div class="page" data-page="{tab_id}"><h2 class="page-title">{e(label)}</h2>')
        for section in sections:
            fields = by_section[section]
            hidden = "" if any(settings.visible(f, values) for f in fields) else " hidden"
            title = f"<h3>{e(section)}</h3>" if len(sections) > 1 else ""
            parts.append(f'<section class="card{hidden}">{title}')
            parts.extend(_field(f, values[f.key], settings.visible(f, values)) for f in fields)
            parts.append(_section_extras(section, values))
            parts.append("</section>")
        parts.append("</div>")
    parts.append('<div class="savebar"><span id="dirty" class="muted"></span>'
                 '<button type="submit">Save settings</button></div></form>')
    parts.append(f'<div class="page" data-page="backup"><h2 class="page-title">Backup</h2>{_BACKUP_CARD}</div>')
    parts.append("</div></div>")
    parts.append(_STATUS_SCRIPT)
    parts.append(_VISIBILITY_SCRIPT)
    parts.append(_TABS_SCRIPT)
    return "".join(parts)


def _field(f: settings.Field, value: str, visible: bool = True) -> str:
    label = f'<label for="{f.key}">{e(f.label)}</label>'
    help_ = f'<small>{e(f.help)}</small>' if f.help else ""
    if f.choices:
        labels = f.labels or f.choices
        opts = "".join(
            f'<option value="{e(c)}"{" selected" if c == value else ""}>{e(lbl)}</option>'
            for c, lbl in zip(f.choices, labels)
        )
        control = f'<select id="{f.key}" name="{f.key}">{opts}</select>'
    elif f.long_text:
        control = f'<textarea id="{f.key}" name="{f.key}" rows="24" class="prompt">{e(value)}</textarea>'
    elif f.multiline:
        control = f'<textarea id="{f.key}" name="{f.key}" rows="3">{e(value)}</textarea>'
    elif f.secret:
        if value:
            hint = f"Saved (ends …{e(value[-4:])}). Leave blank to keep"
            clear = (
                f'<label class="inline"><input type="checkbox" name="clear_{f.key}"> Remove saved value</label>'
            )
        else:
            hint, clear = "Not set", ""
        control = (
            f'<input id="{f.key}" name="{f.key}" type="password" placeholder="{hint}" '
            f'autocomplete="new-password">{clear}'
        )
    else:
        control = f'<input id="{f.key}" name="{f.key}" value="{e(value)}">'
    show_if = (
        f" data-show-if='{e(json.dumps({k: list(v) for k, v in f.show_if}))}'" if f.show_if else ""
    )
    return f'<div class="field"{show_if}{"" if visible else " hidden"}>{label}{control}{help_}</div>'


def _setup_form(error: str = "") -> str:
    err = f'<p class="flash error">{e(error)}</p>' if error else ""
    return (
        f'{err}<form method="post" class="card narrow">'
        f"<p>Welcome. Create the admin password for this page (at least {MIN_PASSWORD} characters).</p>"
        f'<div class="field"><label for="password">New password</label><input id="password" name="password" '
        f'type="password" minlength="{MIN_PASSWORD}" required autofocus autocomplete="new-password"></div>'
        '<div class="field"><label for="confirm">Confirm password</label><input id="confirm" name="confirm" '
        'type="password" required autocomplete="new-password"></div>'
        '<div class="actions"><button type="submit">Create password</button></div></form>'
    )


def _login_form(error: str = "") -> str:
    err = f'<p class="flash error">{e(error)}</p>' if error else ""
    return (
        f'{err}<form method="post" class="card narrow">'
        '<div class="field"><label for="password">Admin password</label>'
        '<input id="password" name="password" type="password" required autofocus></div>'
        '<div class="actions"><button type="submit">Sign in</button></div></form>'
    )


def _page(title: str, body: str, nav: bool = False, wide: bool = False) -> HTMLResponse:
    logout = (
        '<nav><a href="/"' + (' class="active"' if title == "Settings" else "") + '>Settings</a>'
        '<a href="/live"' + (' class="active"' if title == "Live calls" else "") + '>Live calls</a>'
        '<form method="post" action="/logout"><button class="link" type="submit">Sign out</button></form></nav>'
        if nav
        else ""
    )
    return HTMLResponse(
        f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>IT Triage Agent</title><style>{_CSS}</style></head>
<body><header><h1>IT Triage Agent <span>{e(title)}</span></h1>{logout}</header>
<main{' class="wide"' if wide else ' class="settings"' if title == "Settings" else ""}>{body}</main></body></html>""",
        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"},
    )


_LIVE_BODY = """
<p id="conn" class="muted">Connecting…</p>
<div id="calls"><p class="muted" id="empty">No calls yet. Call the agent and the conversation appears here as it happens.</p></div>
<script>
const calls = document.getElementById('calls');
const conn = document.getElementById('conn');
const fmtTime = t => new Date(t * 1000).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'});

function card(ev) {
  let el = document.getElementById('call-' + ev.call_id);
  if (el) return el;
  document.getElementById('empty')?.remove();
  el = document.createElement('section');
  el.className = 'card call';
  el.id = 'call-' + ev.call_id;
  el.innerHTML = '<div class="call-head"><strong class="who">Call</strong> <span class="badge live">Live</span>'
    + '<span class="muted info"></span></div><div class="lines"></div>';
  calls.prepend(el);
  return el;
}

function handle(ev) {
  if (ev.type === 'snapshot') { calls.innerHTML = ''; ev.events.forEach(handle);
    if (!calls.children.length) calls.innerHTML = '<p class="muted" id="empty">No calls yet. Call the agent and the conversation appears here as it happens.</p>';
    return; }
  if (ev.type === 'error') { conn.textContent = ev.text; return; }
  const c = card(ev);
  const lines = c.querySelector('.lines');
  const nearBottom = window.innerHeight + window.scrollY >= document.body.scrollHeight - 80;
  if (ev.type === 'call_start') {
    c.querySelector('.who').textContent = (ev.caller || 'Unknown caller') + (ev.number ? ' <' + ev.number + '>' : '');
    c.querySelector('.info').textContent = fmtTime(ev.time) + ' · ' + ev.info;
  } else if (ev.type === 'call_end') {
    const b = c.querySelector('.badge'); b.textContent = 'Ended ' + fmtTime(ev.time); b.className = 'badge';
  } else if (ev.type === 'note') {
    const n = document.createElement('p'); n.className = 'note'; n.textContent = ev.text; lines.append(n);
  } else if (ev.type === 'line') {
    let l = c.querySelector('[data-line="' + ev.line_id + '"]');
    if (!l) { l = document.createElement('div'); l.dataset.line = ev.line_id; l.className = 'line ' + ev.speaker;
      l.innerHTML = '<span class="speaker"></span><span class="text"></span>'; lines.append(l); }
    l.querySelector('.speaker').textContent = ev.speaker === 'caller' ? 'Caller' : 'Agent';
    l.querySelector('.text').textContent = ev.text;
    l.classList.toggle('partial', !ev.final);
  }
  if (nearBottom && c === calls.firstElementChild) window.scrollTo(0, document.body.scrollHeight);
}

function connect() {
  const src = new EventSource('/live/stream');
  src.onopen = () => { conn.textContent = 'Connected: updates live.'; };
  src.onmessage = m => handle(JSON.parse(m.data));
  src.onerror = () => { conn.textContent = 'Reconnecting…'; };
}
connect();
</script>"""

_BACKUP_CARD = """<section class="card">
<p>Download every setting, including API keys, passwords and the system prompt, as a file. Restore it
here, or on a new server, to put everything back. The admin password and tickets aren't included.</p>
<p class="flash error">The backup contains your API keys and 3CX password in plain text. Store it
somewhere safe, such as a password manager, and don't email it.</p>
<div class="actions split"><a class="button secondary" href="/backup">Download backup</a>
<form method="post" action="/restore" enctype="multipart/form-data" class="restore"
 onsubmit="return confirm('Replace all current settings with this backup?')">
<input type="file" name="file" accept=".json,application/json" required>
<button type="submit" class="secondary">Restore</button></form></div></section>"""

_VISIBILITY_SCRIPT = """<script>
// Show only the settings that apply to the providers selected. Hidden fields keep their values.
function applyVisibility() {
  const val = k => (document.querySelector('[name="' + k + '"]') || {}).value;
  document.querySelectorAll('[data-show-if]').forEach(el => {
    const rules = JSON.parse(el.dataset.showIf);
    el.hidden = !Object.entries(rules).some(([k, allowed]) => allowed.includes(val(k)));
  });
  document.querySelectorAll('form section.card').forEach(sec => {
    const fields = sec.querySelectorAll('.field');
    sec.hidden = fields.length > 0 && [...fields].every(f => f.hidden);
  });
}
document.querySelectorAll('select').forEach(s => s.addEventListener('change', applyVisibility));
applyVisibility();
</script>"""

_TABS_SCRIPT = """<script>
// One form, shown a tab at a time. Hidden tabs are still saved with the form.
const pages = document.querySelectorAll('.page');
const links = document.querySelectorAll('.tabs a');
const savebar = document.querySelector('.savebar');
function showTab(id) {
  if (![...pages].some(p => p.dataset.page === id)) id = 'general';
  pages.forEach(p => { p.hidden = p.dataset.page !== id; });
  links.forEach(a => a.classList.toggle('active', a.dataset.tab === id));
  savebar.hidden = id === 'backup';
  try { sessionStorage.setItem('tab', id); } catch (e) {}
  history.replaceState(null, '', '#' + id);
}
links.forEach(a => a.addEventListener('click', ev => { ev.preventDefault(); showTab(a.dataset.tab); window.scrollTo(0, 0); }));
let start = location.hash.slice(1);
const hint = document.querySelector('[data-tab-hint]');
if (hint && hint.dataset.tabHint) start = hint.dataset.tabHint;
if (!start) { try { start = sessionStorage.getItem('tab') || ''; } catch (e) {} }
showTab(start || 'general');
// Remind people to save before they wander off.
const form = document.getElementById('settings');
let dirty = false;
form.addEventListener('input', () => { dirty = true; document.getElementById('dirty').textContent = 'Unsaved changes'; });
form.addEventListener('submit', () => { dirty = false; });
window.addEventListener('beforeunload', ev => { if (dirty) { ev.preventDefault(); ev.returnValue = ''; } });
</script>"""

_STATUS_SCRIPT = """<script>
async function refresh() {
  try {
    const r = await fetch('/status');
    const s = await r.json();
    document.getElementById('state').textContent = s.state ? s.state[0].toUpperCase() + s.state.slice(1) : 'Unknown';
    document.getElementById('detail').textContent = s.detail || '';
    document.getElementById('dot').className = 'dot ' + (s.state === 'registered' ? 'ok' : s.state === 'unconfigured' ? '' : 'bad');
  } catch (e) {}
}
refresh(); setInterval(refresh, 5000);
</script>"""

_CSS = """
:root { --bg:#f6f7f9; --card:#fff; --text:#1c2230; --muted:#667085; --border:#dde1e7; --accent:#2563eb;
        --ok:#16a34a; --bad:#dc2626; --okbg:#ecfdf3; --badbg:#fef2f2; }
@media (prefers-color-scheme: dark) { :root { --bg:#111418; --card:#1a1f26; --text:#e6e9ef; --muted:#98a2b3;
        --border:#2c333d; --accent:#60a5fa; --okbg:#0f2a1a; --badbg:#2a1414; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:15px/1.5 system-ui, -apple-system, sans-serif; }
header { display:flex; justify-content:space-between; align-items:center; padding:16px 24px;
         border-bottom:1px solid var(--border); background:var(--card); }
h1 { font-size:17px; margin:0; } h1 span { color:var(--muted); font-weight:400; margin-left:8px; }
main { max-width:720px; margin:24px auto; padding:0 16px; }
.card { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:20px; margin-bottom:16px; }
.narrow { max-width:420px; margin:0 auto; }
h2 { font-size:14px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); margin:0 0 12px; }
.field { margin-bottom:14px; } .field:last-child { margin-bottom:0; }
label { display:block; font-weight:600; margin-bottom:4px; }
label.inline { display:inline-flex; gap:6px; font-weight:400; font-size:13px; color:var(--muted); margin-top:4px; }
input:not([type=checkbox]), select, textarea { width:100%; padding:9px 11px; border:1px solid var(--border); border-radius:7px;
         background:var(--bg); color:var(--text); font:inherit; resize:vertical; }
input:focus, select:focus, textarea:focus { outline:2px solid var(--accent); outline-offset:-1px; }
small { display:block; color:var(--muted); margin-top:4px; }
.actions { display:flex; justify-content:flex-end; }
button { background:var(--accent); color:#fff; border:0; border-radius:7px; padding:10px 18px; font:inherit;
         font-weight:600; cursor:pointer; }
button.secondary, a.button.secondary { background:none; color:var(--accent); border:1px solid var(--accent); }
a.button { display:inline-block; border-radius:7px; padding:10px 18px; font-weight:600; text-decoration:none; }
.actions.gap { gap:10px; flex-wrap:wrap; }
.actions.split { justify-content:space-between; flex-wrap:wrap; gap:12px; }
form.restore { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
form.restore input[type=file] { width:auto; padding:6px; }
button.link { background:none; color:var(--muted); padding:0; font-weight:400; }
.flash { padding:12px 14px; border-radius:8px; border:1px solid var(--border); }
.flash.ok { background:var(--okbg); } .flash.error { background:var(--badbg); }
textarea.prompt { font:13px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; }
.muted { color:var(--muted); } code { font-size:13px; }
nav { display:flex; align-items:center; gap:18px; } nav a { color:var(--muted); text-decoration:none; }
nav a.active { color:var(--text); font-weight:600; }
main.wide { max-width:860px; }
.call-head { display:flex; flex-wrap:wrap; align-items:center; gap:8px; margin-bottom:12px; }
.call-head .info { flex-basis:100%; font-size:13px; }
.badge { font-size:12px; border-radius:99px; padding:1px 8px; background:var(--bg); border:1px solid var(--border); color:var(--muted); }
.badge.live { background:var(--ok); border-color:var(--ok); color:#fff; }
.line { display:flex; gap:10px; margin:0 0 8px; }
.line .speaker { flex:0 0 52px; font-size:12px; font-weight:600; color:var(--muted); padding-top:2px; }
.line .text { padding:7px 11px; border-radius:10px; background:var(--bg); border:1px solid var(--border); }
.line.agent .text { background:var(--okbg); }
.line.partial .text { opacity:.6; font-style:italic; }
.note { margin:4px 0 10px 62px; font-size:13px; color:var(--muted); }
.status p { margin:0; }
/* Settings: tabs down the side, one tab's settings at a time */
main.settings { max-width:1040px; }
.layout { display:grid; grid-template-columns:220px minmax(0, 1fr); gap:28px; align-items:start; }
.sidenav { position:sticky; top:16px; }
.sidenav .status { display:flex; gap:2px; align-items:flex-start; background:var(--card); border:1px solid var(--border);
         border-radius:10px; padding:12px; margin-bottom:12px; font-size:14px; }
.sidenav .status .dot { margin-top:6px; flex:0 0 10px; }
.sidenav .status small { margin-top:2px; font-size:12px; line-height:1.4; }
.tabs { display:flex; flex-direction:column; gap:2px; }
.tabs a { color:var(--muted); text-decoration:none; padding:8px 12px; border-radius:7px; }
.tabs a:hover { background:var(--card); color:var(--text); }
.tabs a.active { background:var(--card); color:var(--text); font-weight:600; box-shadow:inset 3px 0 0 var(--accent); }
.page-title { font-size:20px; text-transform:none; letter-spacing:0; color:var(--text); margin:0 0 14px; }
h3 { font-size:13px; text-transform:uppercase; letter-spacing:.04em; color:var(--muted); margin:0 0 12px; }
.content > .flash { margin-top:0; }
.savebar { position:sticky; bottom:0; display:flex; justify-content:flex-end; align-items:center; gap:14px;
         padding:12px 0 16px; background:linear-gradient(transparent, var(--bg) 30%); }
@media (max-width: 760px) {
  .layout { grid-template-columns:minmax(0, 1fr); gap:12px; }
  .sidenav { position:static; }
  .tabs { flex-direction:row; overflow-x:auto; gap:4px; padding-bottom:4px; }
  .tabs a { white-space:nowrap; padding:7px 10px; }
  .tabs a.active { box-shadow:inset 0 -2px 0 var(--accent); }
}
[hidden] { display:none !important; } .dot { display:inline-block; width:10px; height:10px; border-radius:50%;
         background:var(--muted); margin-right:8px; } .dot.ok { background:var(--ok); } .dot.bad { background:var(--bad); }
"""
