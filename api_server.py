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
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.websockets import WebSocket, WebSocketDisconnect

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


async def _lk_retry(coro_fn, retries: int = 3, base_delay: float = 1.0):
    """Exponentieller Backoff für LiveKit API-Calls."""
    for attempt in range(retries):
        try:
            return await coro_fn()
        except Exception as e:
            if attempt == retries - 1:
                raise
            delay = base_delay * (2 ** attempt)
            logger.warning(
                "LiveKit API Fehler (Versuch %d/%d): %s — retry in %.1fs",
                attempt + 1, retries, e, delay,
            )
            await asyncio.sleep(delay)


LIVEKIT_URL = os.getenv("LIVEKIT_URL", "")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "")
AGENT_NAME = os.getenv("LIVEKIT_AGENT_NAME", "lavita-agent")

app = FastAPI(title="LaVita Terminagent API")

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
    salutation: str | None = None


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


@app.post("/twilio/upload-contacts")
async def upload_contacts(file: UploadFile = File(...)):
    """Empfängt eine Excel-Datei und setzt sie als aktive Kontaktliste."""
    if not file.filename or not file.filename.endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="Nur .xlsx/.xls Dateien erlaubt.")

    contacts_dir = os.path.join(os.path.dirname(__file__), "contacts")
    os.makedirs(contacts_dir, exist_ok=True)
    dest_path = os.path.join(contacts_dir, file.filename)

    content = await file.read()
    with open(dest_path, "wb") as f:
        f.write(content)

    # Update env variable to point to new file
    os.environ["CONTACTS_EXCEL_PATH"] = dest_path

    # Clear cache and reload
    from contacts_excel import _contacts_cache, _last_mtime, load_contacts
    _contacts_cache.clear()
    _last_mtime.clear()

    try:
        contacts = load_contacts(excel_path=dest_path)
        return {"message": f"'{file.filename}' hochgeladen. {len(contacts)} Kontakte gefunden.", "contacts": contacts}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Datei konnte nicht gelesen werden: {e}")


@app.post("/twilio/call")
async def start_call(req: CallRequest):
    """Startet einen Anruf über LiveKit SIP."""
    phone = req.to

    # Wenn contact_id angegeben, Nummer aus Kontakten laden
    salutation = req.salutation or ""
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
            salutation = contact.get("salutation") or ""
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
        from livekit.api import LiveKitAPI, CreateSIPParticipantRequest, CreateAgentDispatchRequest, ListParticipantsRequest

        room_name = f"call-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"

        async with LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET) as lk:
            # Agent dispatchen mit Retry — Metadata enthält Name + Anrede als JSON
            import json as _json
            dispatch_meta = _json.dumps({"name": req.name or phone, "salutation": salutation}, ensure_ascii=False)
            dispatch = await _lk_retry(
                lambda: lk.agent_dispatch.create_dispatch(
                    CreateAgentDispatchRequest(agent_name=AGENT_NAME, room=room_name, metadata=dispatch_meta)
                )
            )

            # Aktiv auf Agent im Room warten (max 10s)
            logger.info("Warte auf Agent im Room (max 10s)...")
            agent_ready = False
            for _i in range(10):
                await asyncio.sleep(1)
                try:
                    p_resp = await lk.room.list_participants(ListParticipantsRequest(room=room_name))
                    parts = list(p_resp.participants) if hasattr(p_resp, "participants") else list(p_resp)
                    if parts:
                        logger.info("Agent nach %ds im Room.", _i + 1)
                        agent_ready = True
                        break
                except Exception:
                    pass
            if not agent_ready:
                logger.warning("Agent nicht im Room erkannt nach 10s — starte SIP trotzdem.")

            # SIP-Anruf starten mit Retry
            participant = await _lk_retry(
                lambda: lk.sip.create_sip_participant(
                    CreateSIPParticipantRequest(
                        sip_trunk_id=sip_trunk_id,
                        sip_call_to=phone,
                        room_name=room_name,
                        participant_identity=f"phone-{phone}",
                        participant_name=f"Partner ({req.name or phone})",
                        wait_until_answered=True,
                        play_ringtone=True,
                        max_call_duration={"seconds": 600},
                        ringing_timeout={"seconds": 45},
                    )
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
            resp = await lk.room.list_rooms(ListRoomsRequest(names=[safe_sid]))
            rooms = list(resp.rooms) if hasattr(resp, 'rooms') else list(resp)
            for room in rooms:
                if room.name == safe_sid:
                    # Room with less than 2 participants means call has ended
                    if room.num_participants < 2:
                        return {"status": "completed", "participants": room.num_participants}
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
        from livekit.api import LiveKitAPI, RoomParticipantIdentity, DeleteRoomRequest, ListRoomsRequest, ListParticipantsRequest
        async with LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET) as lk:
            resp = await lk.room.list_rooms(ListRoomsRequest(names=[safe_sid]))
            rooms = list(resp.rooms) if hasattr(resp, 'rooms') else list(resp)
            for room in rooms:
                if room.name == safe_sid:
                    # Alle Participants entfernen
                    p_resp = await lk.room.list_participants(ListParticipantsRequest(room=safe_sid))
                    participants = list(p_resp.participants) if hasattr(p_resp, 'participants') else list(p_resp)
                    for p in participants:
                        await lk.room.remove_participant(
                            RoomParticipantIdentity(room=safe_sid, identity=p.identity)
                        )
                    # Room löschen um sicherzustellen dass alles clean ist
                    try:
                        await lk.room.delete_room(DeleteRoomRequest(room=safe_sid))
                    except Exception:
                        pass
                    return {"status": "hung_up", "room": safe_sid}
            # Room existiert nicht mehr — trotzdem als erfolgreich melden
            return {"status": "hung_up", "room": safe_sid, "note": "room_already_closed"}
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
    logger.info("Latenz empfangen: %.2fs (avg: %.2fs), Clients: %d", data.get("latency", 0), data.get("avg", 0), len(_monitor_clients))
    await _broadcast_monitor({"type": "latency", "latency": data.get("latency", 0), "avg": data.get("avg", 0)})
    return {"ok": True}


@app.get("/monitor/active-call")
async def monitor_active_call():
    """Prüft direkt bei LiveKit ob ein aktiver Anruf-Room existiert.
    Fallback falls der Agent die call-state-Benachrichtigung nicht zustellen konnte."""
    if not LIVEKIT_URL or not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        return {"active": _monitor_call_state.get("active", False), "source": "cache"}

    try:
        from livekit.api import LiveKitAPI, ListRoomsRequest

        async with LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET) as lk:
            resp = await lk.room.list_rooms(ListRoomsRequest())
            rooms = list(resp.rooms) if hasattr(resp, 'rooms') else list(resp)
            for room in rooms:
                if room.name.startswith("call-") and room.num_participants > 0:
                    # Aktiver Anruf gefunden — aktualisiere lokalen State falls veraltet
                    if not _monitor_call_state.get("active"):
                        _monitor_call_state["active"] = True
                        _monitor_call_state["event"] = "call-start"
                        # Room-Name als Hinweis speichern
                        logger.info("Active-Call Polling: Anruf in Room '%s' erkannt (State war veraltet).", room.name)
                        await _broadcast_monitor({"type": "call-start", "active": True, "contact_name": _monitor_call_state.get("contact_name", "")})
                    return {"active": True, "room": room.name, "participants": room.num_participants, "source": "livekit"}

            # Kein aktiver Call — State ggf. korrigieren
            if _monitor_call_state.get("active"):
                _monitor_call_state["active"] = False
                _monitor_call_state["event"] = "call-end"
                logger.info("Active-Call Polling: Kein aktiver Anruf mehr — korrigiere State.")
                await _broadcast_monitor({"type": "call-end", "active": False})
            return {"active": False, "source": "livekit"}
    except Exception as e:
        logger.warning("Active-Call Polling fehlgeschlagen: %s", e)
        return {"active": _monitor_call_state.get("active", False), "source": "cache", "error": str(e)}


@app.get("/twilio/call-history")
async def get_call_history(limit: int = 100):
    """Gibt die letzten Session-Reports zurück."""
    sessions_dir = os.path.join(os.path.dirname(__file__), "sessions")
    if not os.path.isdir(sessions_dir):
        return {"items": []}

    files = sorted(glob.glob(os.path.join(sessions_dir, "session_*.md")), reverse=True)[:limit]
    items = []
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
                content = fh.read(3000)
        except Exception:
            content = ""

        partner = ""
        status = ""
        duration = ""
        summary_preview = ""
        appointment_date = ""
        in_summary = False
        for line in content.split("\n"):
            if "**Partner:**" in line or "- **Partner:**" in line:
                partner = line.split("Partner:**", 1)[1].strip().strip("*").strip()
            elif "**Status:**" in line or "- **Status:**" in line:
                status = line.split("Status:**", 1)[1].strip().strip("*").strip()
            elif "**Ergebnis:**" in line or "- **Ergebnis:**" in line:
                if not status:
                    status = line.split(":**", 1)[1].strip().strip("*").strip()
            elif "**Gesprächsdauer:**" in line or "- **Gesprächsdauer:**" in line:
                duration = line.split(":**", 1)[1].strip().strip("*").strip()
            elif "**Termin:**" in line or "- **Termin:**" in line:
                appointment_date = line.split(":**", 1)[1].strip().strip("*").strip()
            elif "## Zusammenfassung" in line:
                in_summary = True
            elif in_summary and line.strip() and not line.startswith("#"):
                summary_preview = line.strip()[:120]
                in_summary = False

        session_id = fname.replace(".md", "")
        items.append({
            "session_id": session_id,
            "date_time": ts,
            "partner": partner or "Unbekannt",
            "status": status or "unbekannt",
            "duration": duration or "-",
            "summary_preview": summary_preview,
            "appointment_date": appointment_date,
        })
    return {"items": items}


@app.get("/twilio/call-history/{session_id}/audio/{filename}")
async def get_audio_file(session_id: str, filename: str):
    """Liefert eine Audio-Datei aus."""
    safe_filename = re.sub(r"[^a-zA-Z0-9_.\-]", "", filename)
    # Sicherstellen dass die Datei wirklich im sessions-Verzeichnis liegt (kein Traversal)
    sessions_dir = os.path.realpath(os.path.join(os.path.dirname(__file__), "sessions"))
    path = os.path.realpath(os.path.join(sessions_dir, safe_filename))
    if not path.startswith(sessions_dir + os.sep):
        raise HTTPException(status_code=400, detail="Ungültiger Dateipfad")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Audio not found")
    return FileResponse(path, media_type="audio/wav", filename=safe_filename)


@app.get("/twilio/call-history/{session_id}")
async def get_call_detail(session_id: str):
    """Gibt den vollständigen Session-Report zurück."""
    # Sanitize session_id to prevent path traversal
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "", session_id)
    sessions_dir = os.path.join(os.path.dirname(__file__), "sessions")
    path = os.path.join(sessions_dir, f"{safe_id}.md")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Session nicht gefunden")
    try:
        with open(path, encoding="utf-8") as fh:
            content = fh.read()

        # Parse structured data from markdown
        partner = ""
        status = ""
        duration = ""
        appointment_date = ""
        summary_lines = []
        transcript_lines = []
        in_summary = False
        in_transcript = False

        for line in content.split("\n"):
            if "**Partner:**" in line or "- **Partner:**" in line:
                partner = line.split("Partner:**", 1)[1].strip().strip("*").strip()
            elif "**Status:**" in line or "- **Status:**" in line:
                status = line.split("Status:**", 1)[1].strip().strip("*").strip()
            elif "**Ergebnis:**" in line or "- **Ergebnis:**" in line:
                if not status:
                    status = line.split(":**", 1)[1].strip().strip("*").strip()
            elif "**Gesprächsdauer:**" in line or "- **Gesprächsdauer:**" in line:
                duration = line.split(":**", 1)[1].strip().strip("*").strip()
            elif "**Termin:**" in line or "- **Termin:**" in line:
                appointment_date = line.split(":**", 1)[1].strip().strip("*").strip()
            elif "## Zusammenfassung" in line:
                in_summary = True
                in_transcript = False
            elif "## Transkript" in line:
                in_transcript = True
                in_summary = False
            elif line.startswith("## "):
                in_summary = False
                in_transcript = False
            elif in_summary and line.strip():
                summary_lines.append(line.strip())
            elif in_transcript and line.strip():
                transcript_lines.append(line.strip())

        # Extract date from session_id
        m = re.search(r"session_(\d{8})_(\d{6})", safe_id)
        date_time = ""
        if m:
            d, t = m.group(1), m.group(2)
            date_time = f"{d[:4]}-{d[4:6]}-{d[6:8]} {t[:2]}:{t[2:4]}:{t[4:6]}"

        # Check for audio files
        audio_files = []
        # session_id format: "session_20260505_094735" → timestamp is "20260505_094735"
        ts_part = safe_id.replace("session_", "")
        for ext in ("*.wav", "*.mp3"):
            for pattern in (f"{safe_id}*{ext}", f"recording_{ts_part}*{ext}"):
                for af in glob.glob(os.path.join(sessions_dir, pattern)):
                    audio_files.append({
                        "name": os.path.basename(af),
                        "url": f"/twilio/call-history/{safe_id}/audio/{os.path.basename(af)}",
                    })

        return {
            "session_id": safe_id,
            "date_time": date_time,
            "partner": partner or "Unbekannt",
            "status": status or "unbekannt",
            "duration": duration or "-",
            "appointment_date": appointment_date or "-",
            "summary": "\n".join(summary_lines) or "Keine Zusammenfassung vorhanden.",
            "transcript": "\n".join(transcript_lines) or "Kein Transkript vorhanden.",
            "audio_files": audio_files,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
