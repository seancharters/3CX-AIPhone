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
from itertools import groupby

from fastapi import FastAPI, Form, Request
import httpx
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from loguru import logger
from starlette.middleware.sessions import SessionMiddleware

from . import settings
from .asterisk import AMIClient
from .triage import tickets

ADMIN_FILE = settings.DATA_DIR / "admin.json"
SESSION_SECRET_FILE = settings.DATA_DIR / "session_secret"
MIN_PASSWORD = 10


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


# One-time code needed to create the admin password, so whoever reaches the page first
# can't claim it. Printed in the logs: docker compose logs admin
SETUP_CODE = secrets.token_hex(4) if not _password_set() else ""
if SETUP_CODE:
    logger.warning(f"Admin UI first-time setup code: {SETUP_CODE}")

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
        return RedirectResponse("/", status_code=303)
    try:
        await asyncio.to_thread(tickets.send_email, cfg, tickets.test_email(cfg))
        request.session["flash"] = ("ok", f"Saved, and a test email was sent to {cfg['EMAIL_TO']}. Check the inbox (and junk).")
    except Exception as e:
        logger.warning(f"Test email failed: {e!r}")
        request.session["flash"] = ("error", f"Saved, but the test email failed: {type(e).__name__}: {e}")
    return RedirectResponse("/", status_code=303)


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

    return StreamingResponse(relay(), media_type="text/event-stream", headers={"Cache-Control": "no-store"})


@app.get("/setup", response_class=HTMLResponse)
async def setup_page():
    if _password_set():
        return RedirectResponse("/login", status_code=303)
    return _page("First-time setup", _setup_form())


@app.post("/setup", response_class=HTMLResponse)
async def setup(request: Request, code: str = Form(""), password: str = Form(""), confirm: str = Form("")):
    if _password_set():
        return RedirectResponse("/login", status_code=303)
    # No code means the password was removed while running: restart to get a fresh one.
    if not SETUP_CODE or not hmac.compare_digest(code.strip(), SETUP_CODE):
        await asyncio.sleep(1)
        return _page("First-time setup", _setup_form("That setup code isn't right. Find it with: docker compose logs admin"))
    if len(password) < MIN_PASSWORD or password != confirm:
        return _page("First-time setup", _setup_form(f"Passwords must match and be at least {MIN_PASSWORD} characters."))
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


def _settings_form(values: dict[str, str], flash: tuple[str, str] | None) -> str:
    parts = []
    if flash:
        parts.append(f'<p class="flash {e(flash[0])}">{e(flash[1])}</p>')
    parts.append(
        '<section class="card status"><h2>3CX registration</h2>'
        '<p><span id="dot" class="dot"></span><strong id="state">Checking…</strong> '
        '<span id="detail" class="muted"></span></p></section>'
    )
    parts.append('<form method="post" autocomplete="off">')
    for section, fields in groupby(settings.FIELDS, key=lambda f: f.section):
        parts.append(f'<section class="card"><h2>{e(section)}</h2>')
        for f in fields:
            parts.append(_field(f, values[f.key]))
        if section == "Email tickets":
            parts.append(
                '<div class="actions"><button type="submit" formaction="/test-email" class="secondary">'
                "Save and send test email</button></div>"
            )
        parts.append("</section>")
    parts.append('<div class="actions"><button type="submit">Save settings</button></div></form>')
    parts.append(_STATUS_SCRIPT)
    return "".join(parts)


def _field(f: settings.Field, value: str) -> str:
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
    return f'<div class="field">{label}{control}{help_}</div>'


def _setup_form(error: str = "") -> str:
    err = f'<p class="flash error">{e(error)}</p>' if error else ""
    return (
        f'{err}<form method="post" class="card narrow">'
        "<p>Create the admin password. You'll need the one-time setup code from the server:<br>"
        "<code>docker compose logs admin</code></p>"
        '<div class="field"><label for="code">Setup code</label><input id="code" name="code" required></div>'
        f'<div class="field"><label for="password">New password</label><input id="password" name="password" '
        f'type="password" minlength="{MIN_PASSWORD}" required autocomplete="new-password"></div>'
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
<main{' class="wide"' if wide else ""}>{body}</main></body></html>""",
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
button.secondary { background:none; color:var(--accent); border:1px solid var(--accent); }
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
.status p { margin:0; } .dot { display:inline-block; width:10px; height:10px; border-radius:50%;
         background:var(--muted); margin-right:8px; } .dot.ok { background:var(--ok); } .dot.bad { background:var(--bad); }
"""
