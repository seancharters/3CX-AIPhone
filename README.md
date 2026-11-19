# AI IT Triage Phone Agent

A voice agent that answers IT support calls as a **3CX extension**, triages the issue,
creates a ticket, and transfers P1s (or anyone who asks) to a human. Everything runs in
Docker, and API keys and 3CX details are entered in a small admin web page.

```
Caller ─► 3CX ─► ext 800 ═══ SIP ═══► asterisk container ── media WebSocket ──► agent container
           ▲                             │                                        │
           └──── transfer (SIP REFER) ◄──┘◄────────── AMI redirect ───────────────┤
                                               Azure Speech ─► Azure OpenAI ─► ElevenLabs
                                                                      └─► create_ticket / transfer / end_call
```

| Container | What it does |
|---|---|
| `asterisk` | Asterisk 22 LTS. Registers with 3CX as an extension (like a desk phone), answers calls, and streams audio to the agent. Performs transfers back to 3CX. |
| `agent` | Python ([Pipecat](https://github.com/pipecat-ai/pipecat)). Speech-to-text (Azure AI Speech, Azure OpenAI gpt-realtime-whisper or Deepgram) → LLM (Azure OpenAI or Claude) → text-to-speech (ElevenLabs), plus the triage tools. Not exposed to the internet. |
| `admin` | Password-protected settings page: API keys, 3CX extension, voice/model, and live registration status. Only reachable from the server itself (use an SSH tunnel). |

Settings are stored in the `data` Docker volume. The agent reads them at the start of every call, and
Asterisk re-registers with 3CX a few seconds after you save, so nothing needs restarting.

| File | What to change there |
|---|---|
| `agent/app/triage/prompts.py` | The agent's persona, questions, priority rules and boundaries. **Tune this first.** |
| `agent/app/triage/tickets.py` | Tickets: saved as JSON in a Docker volume, and optionally emailed. Add your PSA here later. |
| `agent/app/bot.py` | Voice pipeline and tools (`create_ticket`, `transfer_to_human`, `end_call`). |
| `agent/app/admin.py`, `settings.py` | The admin page and the list of settings it manages. |
| `asterisk/config/*.conf` | Asterisk config templates. `${VARS}` are filled in from the admin settings. |

## 1. Create the extension in 3CX

In the 3CX admin console (v20):
1. **Users → Add**. Create a user for the agent, e.g. extension `800`, first name "IT Support AI".
2. Open the user's **IP Phone / provisioning** details and note the **Auth ID** and **Auth password**.
   (The Auth ID is *not* the extension number.)
3. The agent's server connects from outside your office, so in the user's security options,
   allow the extension to be used outside the LAN (untick "Disallow use of extension outside the LAN").
4. Decide where human transfers go: an engineer's extension, a ring group or a queue (e.g. `810`).

## 2. Deploy

Any Linux VM with Docker works. Put it in Sydney (e.g. AWS Lightsail/EC2 `ap-southeast-2`,
2 vCPU / 2 GB) so callers hear fast responses.

**Firewall:** no inbound ports are required. Asterisk registers *out* to 3CX and keeps that
connection alive, so incoming calls and call audio come back as replies through the server's
stateful firewall (e.g. an Azure NSG or AWS security group). Just allow outbound UDP to your 3CX
(`5060` and its RTP ports) and HTTPS to the AI providers, which most clouds allow by default.
Reach the server for admin over SSH (or Azure Bastion), ideally limited to your own IP.

```bash
git clone <this repo> ai-phone && cd ai-phone
docker compose up -d --build       # first build compiles Asterisk: ~5-10 min
```

## 3. Configure in the admin page

The admin page is only published on the server's localhost, because it holds your API keys.
From your laptop, open an SSH tunnel and browse to it:

```bash
ssh -L 8000:localhost:8000 you@your-server
# then open http://localhost:8000
```

1. **First visit:** you're asked to create an admin password.
2. Fill in:
   - **Providers:** speech-to-text and AI model (Azure by default; Deepgram and Claude are alternatives)
   - **Azure:** Azure OpenAI endpoint, key and deployment name; Speech region (and key, if it's a
     different resource)
   - **Voice:** ElevenLabs API key and voice
   - **3CX extension:** FQDN (e.g. `yourcompany.3cx.com.au`), extension, Auth ID, Auth password, and
     where transfers go
   - **Server:** this server's public IP
   - **Agent:** company name and the greeting the agent answers with
   - **System prompt** (optional): how the agent behaves. Pre-filled with the default
   - **Email tickets** (optional): SMTP details and where to send tickets. Use **Save and send test
     email** to check them
3. Save. The **3CX registration** box at the top turns green ("Registered") within a few seconds,
   and extension 800 shows as online in 3CX.

The **Live calls** tab shows each call as it happens: what the caller says (grey italic while
they're still speaking), what the agent says, and actions like tickets and transfers. It keeps the
last 10 calls in memory (cleared when the agent restarts; nothing is written to disk).

Saved keys are never shown again. Leave a key field blank to keep the stored value.
Forgot the admin password? `docker compose exec admin rm /data/admin.json && docker compose restart admin`,
then open the page again to create a new one.

Then **call extension 800** from any 3CX phone. To put it in front of customers, add an option to
your 3CX **Digital Receptionist** (e.g. "press 1 for IT support") that goes to extension 800.

- **Update:** `git pull && docker compose up -d --build`
- **Tickets:** `docker compose exec agent ls tickets`, or copy them out with
  `docker compose cp agent:/srv/tickets ./tickets`

## Testing without 3CX (optional)

Set a **Test softphone password** in the admin page. Then register a softphone (e.g. Zoiper or Linphone)
to the server with user `tester` and that password, and dial `800`. To try this on your Mac,
run the stack locally, set the server public IP to your Mac's LAN IP, and point the softphone at it.

## Choosing providers

All switchable in the admin page, taking effect on the next call:

| | Options | Notes |
|---|---|---|
| Speech-to-text | **Azure AI Speech** | Australian English (`en-AU`) model; can run in Australia East so caller audio stays in Australia |
| | Azure OpenAI **gpt-realtime-whisper** | Streaming Whisper. Language hint is just `en`. The "delay" setting trades speed for accuracy. Check which region your deployment processes in |
| | Deepgram | `nova-3`, `en-AU` |
| Voice | **ElevenLabs** | Most natural. The free plan is 10,000 characters/month (a few calls) and can't use library voices |
| | **Azure AI Speech** | Australian neural voices (Natasha, William, …), billed to Azure per character. Uses the Speech region/key |
| AI model | **Azure OpenAI** | Use a fast, non-reasoning deployment such as `gpt-4.1` or `gpt-4.1-mini`; reasoning models add seconds of silence |
| | Anthropic Claude | `claude-opus-5` at low effort by default; Sonnet/Haiku are faster. Server-side refusal fallbacks enabled |

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Admin page shows `Rejected` | Wrong Auth ID/password (use the Auth ID, not the extension number), or 3CX blocking use outside the LAN |
| Call connects but one-way or no audio | Server public IP wrong in the admin page, or outbound UDP to 3CX's RTP ports blocked |
| Call answers then hangs up straight away | An API key is wrong. Check `docker compose logs agent` |
| Transfer doesn't reach the engineer | Check "Transfer calls to" in the admin page. If 3CX refuses the SIP transfer, Asterisk falls back to dialling the target through 3CX |
| Engineer doesn't hear the briefing | "Transfer type" is Announced, which dials the target through 3CX and briefs whoever answers first. 3CX queues answer before an engineer picks up, so use Blind for queues |

```bash
docker compose exec asterisk asterisk -rvvv                        # Asterisk console
docker compose exec asterisk asterisk -rx "pjsip set logger on"    # log SIP traffic
```

## Email tickets

With **Email tickets** on, each ticket is emailed as soon as the agent creates it:
- Subject: `[P3] IT-7KQ2M: Outlook crashes on open (Acme Pty Ltd)`
- Body: priority, category, caller details, summary, description and the call transcript (HTML and plain text)
- Reply-To is the caller's email, so replying goes straight to them

Tickets are always saved on the server too, so a mail problem never loses one (it's logged in
`docker compose logs agent`). For Microsoft 365: `smtp.office365.com`, port 587, STARTTLS, and SMTP AUTH
enabled on the sending mailbox.

## Helpdesk / PSA (optional)

Under **Tickets → Helpdesk (PSA)** choose **ConnectWise PSA** or **HaloPSA**. Tickets are then created in
the PSA and the agent reads out the PSA's ticket number. Every ticket is also saved on the server first,
so if the PSA can't be reached the caller still gets a reference (starting `IT-`) and the failure shows in
**Live calls**.

- **Caller recognition:** when a call is answered, the caller ID is looked up. If it matches a contact,
  the agent asks for the caller's name as usual and, if it matches, confirms their organisation and
  email instead of asking for them. Numbers match on their last 9 digits, so `+61 412 345 678` and
  `0412345678` are the same.
- **Linking:** the ticket is linked to the caller ID's contact if the caller gave that contact's name;
  otherwise to the contact with the email address the caller gave; otherwise to the caller ID's company;
  otherwise to the **unknown callers** company/client you choose.
- The call transcript goes in an internal note (ConnectWise: Internal Analysis; Halo: a private note).
- **Dropped calls:** if a call ends before a ticket is logged (the caller hangs up or the line drops), a
  P3 "call back" ticket is logged with their caller ID and everything they said. Calls the agent ended
  itself or transferred are skipped. Set **Tickets → Dropped calls** to also cover hang-ups during the
  greeting, or turn it off. This works without a PSA too: the ticket is saved and emailed as usual.
- **Save and test connection** lists your boards/ticket types and priorities with their IDs, and can
  look up a test number.

**ConnectWise:** System → Members → API Members → add a member with a security role that can read
companies and contacts and add service tickets, then create an API key. You also need a free Client ID
from developer.connectwise.com. The site is your region's API host, e.g. `api-au.myconnectwise.net`.

**HaloPSA:** Configuration → Integrations → HaloPSA API → View Applications → New. Authentication method
**Client ID and Secret (Services)**, logged in as an agent who can see all clients, with the
`read:customers` and `edit:tickets` permissions. Create a client such as "Unknown Callers" for callers
who aren't in Halo, and enter its ID.

## Password resets (optional)

With **Password resets** on, the agent can reset a caller's Microsoft 365 / Entra password once it
has verified them:
1. It looks up their account by work email (or full name).
2. **Employee ID on the account?** The caller must say it (3 attempts).
   **No employee ID?** A six-digit code is texted via SMTP2GO to the mobile number *on the account*
   (never a number the caller gives). 3 attempts per code, 2 codes per call, codes expire in 10 minutes.
3. It sets a temporary password (e.g. `Maple-River-4827`) that must be changed at next sign-in, and
   reads it to the caller twice. The password is never shown to the AI model, written to tickets or
   shown in Live calls. Employee IDs and SMS codes are removed from ticket transcripts.

Every check is enforced in code (`agent/app/identity.py`), not by the AI. Accounts with any Entra admin
role, disabled accounts and on-premises synced accounts are never reset by phone. Five failed
verifications in an hour, or three resets in a day, pause phone resets for that account. Every attempt
is logged to `/srv/tickets/password-resets.log` in the agent container.

**Entra setup:** Entra admin centre → App registrations → New registration. Under **API permissions**
add Microsoft Graph *Application* permissions `User.Read.All`, `User-PasswordProfile.ReadWrite.All`
and `RoleManagement.Read.Directory` (plus `GroupMember.Read.All` if you restrict resets to a group, and
`UserAuthenticationMethod.Read.All` if SMS codes go to MFA numbers), then **Grant admin consent**.
Create a client secret. Then **Roles and administrators → Helpdesk Administrator → Add assignments**
and add the app. Enter the tenant ID, client ID and secret in the admin page and use **Save and test Entra**.

**SMS:** create an SMTP2GO API key with SMS access, enter it, and use **Save and send test SMS**.

## Next steps

- [ ] Tune the system prompt in the admin page: priority rules, what to ask for common issues
- [ ] Add a PSA integration in `tickets.py` alongside email
- [ ] Email or Teams notification on P1 tickets
- [ ] Look up the caller's number/email against your customer list to pre-fill organisation
- [ ] After-hours behaviour (ticket only, or on-call transfer)
- [ ] Call recording/transcript retention policy (Australian Privacy Act)
