"""WebSocket server that Asterisk connects to for each call (see asterisk/config/websocket_client.conf)."""

from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI, WebSocket
from fastapi.responses import StreamingResponse
from loguru import logger

from . import settings
from .asterisk import AMIClient
from .bot import CallInfo, run_call
from .live import bus

app = FastAPI()

ami = AMIClient(host="asterisk", port=5038, username="agent", secret=settings.ami_secret())


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.websocket("/media")
async def media(websocket: WebSocket) -> None:
    # Asterisk passes call details as URI params via v(...) in the dialplan.
    q = websocket.query_params
    call = CallInfo(
        caller_number=q.get("caller_num", ""),
        caller_name=q.get("caller_name", ""),
        channel=q.get("channel", ""),
        codec=q.get("codec", ""),
    )
    # Phone-line codec: g722 is wideband (HD) audio; ulaw/alaw are narrowband and harder to transcribe.
    logger.info(f"Call audio codec from 3CX: {q.get('codec', 'unknown')}")
    await websocket.accept(subprotocol="media")
    try:
        await run_call(websocket, call, ami)
    except Exception:
        logger.exception(f"Call on {call.channel} failed")


@app.get("/events")
async def events() -> StreamingResponse:
    """Live call events for the admin UI (internal network only; the agent port isn't published)."""

    async def stream():
        queue = bus.subscribe()
        try:
            yield f"data: {json.dumps({'type': 'snapshot', 'events': bus.snapshot()})}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {json.dumps(event)}\n\n"
                except TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(stream(), media_type="text/event-stream")
