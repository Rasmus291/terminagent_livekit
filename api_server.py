"""
Einfacher API-Server für das LaVita Terminagent Frontend.
Stellt Endpoints für Kontakte und Anrufe via LiveKit SIP bereit.
Enthält WebSocket-Relay für Live-Audio-Monitor.

Starten: python api_server.py
"""

import asyncio
import base64
import datetime
import glob
import logging
import os
import re

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.websockets import WebSocket, WebSocketDisconnect

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

LIVEKIT_URL = os.getenv("LIVEKIT_URL", "")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "")
AGENT_NAME = os.getenv("LIVEKIT_AGENT_NAME", "lavita-agent")

app = FastAPI(title="LaVita Terminagent API")

# Serve session audio files (WAV recordings)
_sessions_dir = os.path.join(os.path.dirname(__file__), "sessions")
os.makedirs(_sessions_dir, exist_ok=True)
app.mount("/sessions", StaticFiles(directory=_sessions_dir), name="sessions")

# ── Live-Audio-Monitor: WebSocket-Relay ──────────────────────────────────
# Browser verbinden sich per WebSocket, Agent sendet Audio per HTTP POST.
_monitor_clients: set[WebSocket] = set()
_monitor_call_state: dict = {"active": False, "contact_name": "", "caller_id": ""}


async def _broadcast_monitor(data: dict):
    """Sendet JSON-Daten an alle verbundenen Monitor-Clients."""
    dead = set()
    import json
    msg = json.dumps(data)
    for ws in _monitor_clients:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    _monitor_clients.difference_update(dead)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class CallRequest(BaseModel):
    to: str | None = None
    name: str | None = None
    contact_id: str | None = None
    sip_trunk_id: str | None = None


async def _resolve_sip_trunk_id(req: CallRequest) -> str:
    """Ermittelt SIP-Trunk-ID aus Request, Env oder LiveKit Auto-Discovery."""
    # 1) explizit im Request
    if req.sip_trunk_id:
        return req.sip_trunk_id

    # 2) bekannte Env-Varianten
    for key in ("LIVEKIT_SIP_TRUNK_ID", "LIVEKIT_OUTBOUND_TRUNK_ID", "SIP_TRUNK_ID"):
        value = os.getenv(key, "").strip()
        if value:
            return value

    # 3) Auto-Discovery: wenn genau 1 Outbound-Trunk vorhanden ist, verwende ihn
    try:
        from livekit.api import LiveKitAPI, ListSIPOutboundTrunkRequest

        async with LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET) as lk:
            resp = await lk.sip.list_outbound_trunk(ListSIPOutboundTrunkRequest())
            trunks = list(getattr(resp, "items", []) or [])
            if len(trunks) == 1:
                trunk = trunks[0]
                trunk_id = (getattr(trunk, "sip_trunk_id", "") or "").strip()
                if trunk_id:
                    logger.info("Auto-Discovery: verwende einzigen SIP-Trunk %s", trunk_id)
                    return trunk_id
    except Exception as e:
        logger.warning("SIP-Trunk Auto-Discovery fehlgeschlagen: %s", e)

    return ""


@app.get("/twilio/contacts")
async def get_contacts():
    """Lädt Kontakte aus der Excel-Datei."""
    try:
        from contacts_excel import load_contacts
        contacts = load_contacts()
        return {"contacts": contacts}
    except Exception as e:
        logger.error("Kontakte laden fehlgeschlagen: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/twilio/call")
async def start_call(req: CallRequest):
    """Startet einen Anruf über LiveKit SIP."""
    phone = req.to

    # Wenn contact_id angegeben, Nummer aus Kontakten laden
    if req.contact_id and not phone:
        try:
            from contacts_excel import load_contacts
            contacts = load_contacts()
            contact = next((c for c in contacts if str(c.get("contact_id")) == req.contact_id), None)
            if not contact:
                raise HTTPException(status_code=404, detail=f"Kontakt {req.contact_id} nicht gefunden")
            phone = contact.get("phone")
            if not phone:
                raise HTTPException(status_code=400, detail="Kontakt hat keine Telefonnummer")
            # Name aus Kontakt übernehmen falls nicht explizit angegeben
            if not req.name:
                req.name = contact.get("last_name") or contact.get("name") or ""
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    if not phone:
        raise HTTPException(status_code=400, detail="Telefonnummer fehlt")

    sip_trunk_id = await _resolve_sip_trunk_id(req)
    if not sip_trunk_id:
        raise HTTPException(
            status_code=500,
            detail="SIP-Trunk nicht konfiguriert. Setze LIVEKIT_SIP_TRUNK_ID oder übergib sip_trunk_id im Request.",
        )

    try:
        from livekit.api import LiveKitAPI, CreateSIPParticipantRequest, CreateAgentDispatchRequest

        room_name = f"call-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"

        async with LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET) as lk:
            # Agent dispatchen
            dispatch = await lk.agent_dispatch.create_dispatch(
                CreateAgentDispatchRequest(agent_name=AGENT_NAME, room=room_name)
            )

            # SIP-Anruf starten
            participant = await lk.sip.create_sip_participant(
                CreateSIPParticipantRequest(
                    sip_trunk_id=sip_trunk_id,
                    sip_call_to=phone,
                    room_name=room_name,
                    participant_identity=f"phone-{phone}",
                    participant_name=f"Partner ({req.name or phone})",
                    wait_until_answered=True,
                    play_ringtone=True,
                    max_call_duration={"seconds": 600},
                )
            )

            logger.info("Anruf gestartet: %s → Room %s", phone, room_name)
            return {
                "status": "calling",
                "to": phone,
                "room": room_name,
                "call_sid": room_name,  # Room-Name als Call-ID für Status-Polling
                "sip_call_id": participant.sip_call_id,
            }
    except Exception as e:
        logger.error("Anruf fehlgeschlagen: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/twilio/call-status/{call_sid}")
async def get_call_status(call_sid: str):
    """Prüft ob ein Anruf (Room) noch aktiv ist."""
    safe_sid = re.sub(r"[^a-zA-Z0-9_-]", "", call_sid)
    try:
        from livekit.api import LiveKitAPI, ListRoomsRequest
        async with LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET) as lk:
            rooms_resp = await lk.room.list_rooms(ListRoomsRequest(names=[safe_sid]))
            for room in rooms_resp.rooms:
                if room.name == safe_sid:
                    # Room existiert = Anruf läuft (auch wenn noch keine Participants)
                    return {"status": "in-progress", "participants": room.num_participants}
            return {"status": "completed"}
    except Exception as e:
        logger.warning("Call-Status-Abfrage fehlgeschlagen: %s", e)
        return {"status": "unknown"}


@app.post("/twilio/hangup")
async def hangup_call(req: dict):
    """Beendet einen aktiven Anruf."""
    call_sid = req.get("call_sid", "")
    if not call_sid:
        raise HTTPException(status_code=400, detail="call_sid fehlt")
    safe_sid = re.sub(r"[^a-zA-Z0-9_-]", "", call_sid)
    try:
        from livekit.api import LiveKitAPI, RoomParticipantIdentity, ListRoomsRequest, ListParticipantsRequest
        async with LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET) as lk:
            rooms_resp = await lk.room.list_rooms(ListRoomsRequest(names=[safe_sid]))
            for room in rooms_resp.rooms:
                if room.name == safe_sid:
                    # Alle Participants entfernen
                    pts_resp = await lk.room.list_participants(ListParticipantsRequest(room=safe_sid))
                    for p in pts_resp.participants:
                        await lk.room.remove_participant(
                            RoomParticipantIdentity(room=safe_sid, identity=p.identity)
                        )
                    return {"status": "hung_up", "room": safe_sid}
            return {"status": "not_found"}
    except Exception as e:
        logger.error("Hangup fehlgeschlagen: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# Frontend ausliefern
@app.get("/")
async def serve_frontend():
    return FileResponse(os.path.join(os.path.dirname(__file__), "frontend", "index.html"))


# ── Monitor WebSocket (Browser) ─────────────────────────────────────────
@app.websocket("/monitor/ws")
async def monitor_ws(ws: WebSocket):
    """WebSocket für Browser-Clients zum Live-Mithören."""
    await ws.accept()
    _monitor_clients.add(ws)
    logger.info("Monitor-Client verbunden. Clients: %d", len(_monitor_clients))
    try:
        # Sende aktuellen Call-State
        await ws.send_json({"type": "state", **_monitor_call_state})
        # Halte Verbindung offen — Daten kommen über _broadcast_monitor
        while True:
            # Warte auf Client-Nachrichten (Ping/Pong oder Close)
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _monitor_clients.discard(ws)
        logger.info("Monitor-Client getrennt. Clients: %d", len(_monitor_clients))


# ── Monitor HTTP API (Agent → Server) ───────────────────────────────────
class MonitorAudioChunk(BaseModel):
    track: str  # "partner" oder "agent"
    sample_rate: int = 16000
    pcm16_b64: str  # Base64-encoded PCM16 LE


@app.post("/monitor/audio")
async def monitor_audio(chunk: MonitorAudioChunk):
    """Empfängt Audio-Chunks vom Agent und leitet sie an Browser weiter."""
    if not _monitor_clients:
        return {"relayed": 0}
    await _broadcast_monitor({
        "type": "audio",
        "track": chunk.track,
        "sample_rate": chunk.sample_rate,
        "pcm16": chunk.pcm16_b64,
    })
    return {"relayed": len(_monitor_clients)}


@app.post("/monitor/call-state")
async def monitor_call_state(state: dict):
    """Aktualisiert den Call-State (call-start / call-end)."""
    _monitor_call_state.update(state)
    await _broadcast_monitor({"type": state.get("event", "state"), **state})
    return {"ok": True}


@app.post("/monitor/latency")
async def monitor_latency(data: dict):
    """Empfängt Audio-Latenz-Messungen und sendet sie an die UI."""
    await _broadcast_monitor({"type": "latency", "latency": data.get("latency", 0), "avg": data.get("avg", 0)})
    return {"ok": True}


@app.get("/twilio/call-history")
async def get_call_history(limit: int = 5):
    """Gibt die letzten Session-Reports zurück."""
    sessions_dir = os.path.join(os.path.dirname(__file__), "sessions")
    if not os.path.isdir(sessions_dir):
        return {"sessions": []}

    files = sorted(glob.glob(os.path.join(sessions_dir, "session_*.md")), reverse=True)[:limit]
    sessions = []
    for f in files:
        fname = os.path.basename(f)
        # Extract timestamp from filename: session_YYYYMMDD_HHMMSS.md
        m = re.search(r"session_(\d{8})_(\d{6})", fname)
        ts = ""
        if m:
            d, t = m.group(1), m.group(2)
            ts = f"{d[:4]}-{d[4:6]}-{d[6:8]} {t[:2]}:{t[2:4]}:{t[4:6]}"

        # Parse basic info from file
        try:
            with open(f, encoding="utf-8") as fh:
                content = fh.read(2000)
        except Exception:
            content = ""

        partner = ""
        status = ""
        for line in content.split("\n"):
            if "Partner:" in line:
                partner = line.split("Partner:", 1)[1].strip().strip("*")
            elif "Status:" in line or "Ergebnis:" in line:
                status = line.split(":", 1)[1].strip().strip("*")

        sessions.append({
            "id": fname.replace(".md", ""),
            "timestamp": ts,
            "partner": partner,
            "status": status,
            "filename": fname,
        })
    return {"sessions": sessions}


@app.get("/twilio/call-history/{session_id}")
async def get_call_detail(session_id: str):
    """Gibt den vollständigen Session-Report zurück, aufbereitet für das Frontend."""
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", session_id)
    sessions_dir = os.path.join(os.path.dirname(__file__), "sessions")
    path = os.path.join(sessions_dir, f"{safe_id}.md")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Session nicht gefunden")
    try:
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # ── Parse markdown into structured fields ──────────────────────────
    partner = ""
    status = ""
    date_time = ""
    duration = ""
    appointment_date = ""
    summary_lines: list[str] = []
    transcript_lines: list[str] = []

    in_summary = False
    in_transcript = False

    for line in content.split("\n"):
        stripped = line.strip()

        # Section headers
        if stripped.startswith("## "):
            section = stripped[3:].lower()
            in_summary = "zusammenfassung" in section
            in_transcript = "transkript" in section
            continue

        # Key-value lines: - **Key:** Value
        kv = re.match(r"-\s+\*\*([^*]+)\*\*[:\s]+(.+)", stripped)
        if kv:
            key = kv.group(1).strip().lower()
            value = kv.group(2).strip()
            if "partner" in key and not partner:
                partner = value
            elif key in ("status", "ergebnis") and not status:
                status = value
            elif "datum" in key or "uhrzeit" in key:
                date_time = value
            elif "dauer" in key or "länge" in key or "gespräch" in key:
                duration = value
            elif "termin" in key and "datum" not in key:
                appointment_date = value
            in_summary = False
            in_transcript = False
            continue

        if in_summary and stripped and not stripped.startswith("*Kein"):
            summary_lines.append(stripped)
        elif in_transcript and stripped and not stripped.startswith("*Kein"):
            transcript_lines.append(stripped)

    summary = " ".join(summary_lines).strip()
    transcript = "\n".join(transcript_lines).strip()

    # ── Find matching audio files ──────────────────────────────────────
    # Timestamp embedded in session id: session_YYYYMMDD_HHMMSS
    # Audio files: recording_YYYYMMDD_HHMMSS.wav, recording_user_YYYYMMDD_HHMMSS.wav, etc.
    ts_match = re.search(r"session_(\d{8}_\d{6})", safe_id)
    audio_files = []
    if ts_match:
        ts = ts_match.group(1)
        for fname in os.listdir(sessions_dir):
            if fname.endswith(".wav") and ts in fname:
                audio_files.append({"name": fname, "url": f"/sessions/{fname}"})

    return {
        "id": safe_id,
        "partner": partner,
        "status": status,
        "date_time": date_time,
        "duration": duration,
        "appointment_date": appointment_date,
        "summary": summary,
        "transcript": transcript,
        "audio_files": audio_files,
        "content": content,  # raw markdown as fallback
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
