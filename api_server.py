"""
Einfacher API-Server für das LaVita Terminagent Frontend.
Stellt Endpoints für Kontakte und Anrufe via LiveKit SIP bereit.

Starten: python api_server.py
"""

import asyncio
import datetime
import glob
import logging
import os
import re

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from livekit.api import DeleteRoomRequest, ListParticipantsRequest, ListRoomsRequest, LiveKitAPI, RoomParticipantIdentity
from pydantic import BaseModel

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

LIVEKIT_URL = os.getenv("LIVEKIT_URL", "")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "")
SIP_TRUNK_ID = os.getenv("LIVEKIT_SIP_TRUNK_ID", "")
AGENT_NAME = os.getenv("LIVEKIT_AGENT_NAME", "lavita-agent")
_sessions_dir = os.path.join(os.path.dirname(__file__), "sessions")
_active_calls: dict[str, dict] = {}

app = FastAPI(title="LaVita Terminagent API")

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


def _extract_section(markdown_text: str, title: str) -> str:
    pattern = rf"^##\s+{re.escape(title)}\s*$"
    lines = markdown_text.splitlines()
    start_index = None
    for idx, line in enumerate(lines):
        if re.match(pattern, line.strip()):
            start_index = idx + 1
            break
    if start_index is None:
        return ""

    collected = []
    for line in lines[start_index:]:
        if line.startswith("## "):
            break
        collected.append(line)
    return "\n".join(collected).strip()


def _parse_session_report(session_path: str) -> dict:
    filename = os.path.basename(session_path)
    session_id = filename.replace("session_", "").replace(".md", "")

    try:
        with open(session_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        content = ""

    summary = _extract_section(content, "Zusammenfassung")
    transcript = _extract_section(content, "Transkript")
    call_details = _extract_section(content, "Anruf-Details")
    term_data = _extract_section(content, "Termindaten")

    def _pick(pattern: str, source: str) -> str:
        match = re.search(pattern, source, re.IGNORECASE)
        return match.group(1).strip() if match else ""

    date_time = _pick(r"\*\*Datum\s*&\s*Uhrzeit:\*\*\s*(.+)", call_details)
    duration = _pick(r"\*\*Gespr[aä]chsdauer:\*\*\s*(.+)", call_details)
    partner = _pick(r"\*\*Partner:\*\*\s*(.+)", term_data)
    status = _pick(r"\*\*Status:\*\*\s*(.+)", term_data)
    appointment_date = _pick(r"\*\*Termin:\*\*\s*(.+)", term_data)

    if not partner:
        user_match = re.search(r"\*\*\[[^\]]+\]\s*User:\*\*\s*(.+)", transcript)
        if user_match:
            partner = user_match.group(1).strip()[:80]

    audio_files = []
    for prefix in ["recording_", "recording_user_", "recording_agent_"]:
        audio_name = f"{prefix}{session_id}.wav"
        full_path = os.path.join(_sessions_dir, audio_name)
        if os.path.exists(full_path):
            audio_files.append(
                {
                    "name": audio_name,
                    "url": f"/media/sessions/{audio_name}",
                }
            )

    return {
        "session_id": session_id,
        "filename": filename,
        "date_time": date_time,
        "duration": duration,
        "partner": partner,
        "status": status,
        "appointment_date": appointment_date,
        "summary": summary,
        "summary_preview": (summary[:180] + "…") if len(summary) > 180 else summary,
        "transcript": transcript,
        "audio_files": audio_files,
    }


async def _cleanup_stale_call_rooms(lk: LiveKitAPI) -> None:
    """Entfernt alte/aktive call-* Räume inklusive Teilnehmer (Zombie-Call-Schutz)."""
    try:
        rooms_response = await lk.room.list_rooms(ListRoomsRequest())
    except Exception as e:
        logger.warning("Room-Liste konnte nicht geladen werden (Zombie-Cleanup übersprungen): %s", e)
        return

    for room in getattr(rooms_response, "rooms", []) or []:
        room_name = getattr(room, "name", "")
        if not room_name.startswith("call-"):
            continue

        try:
            participants_response = await lk.room.list_participants(
                ListParticipantsRequest(room=room_name)
            )
            for participant in getattr(participants_response, "participants", []) or []:
                identity = getattr(participant, "identity", "")
                if identity:
                    await lk.room.remove_participant(
                        RoomParticipantIdentity(room=room_name, identity=identity)
                    )
                    logger.info("Zombie-Cleanup: Participant %s aus Room %s entfernt", identity, room_name)

            await lk.room.delete_room(DeleteRoomRequest(room=room_name))
            logger.info("Zombie-Cleanup: Room %s gelöscht", room_name)
        except Exception as e:
            logger.warning("Zombie-Cleanup für Room %s fehlgeschlagen: %s", room_name, e)


async def _hangup_livekit_call(lk: LiveKitAPI, room_name: str) -> None:
    participants_response = await lk.room.list_participants(ListParticipantsRequest(room=room_name))
    for participant in getattr(participants_response, "participants", []) or []:
        identity = getattr(participant, "identity", "")
        if identity:
            await lk.room.remove_participant(RoomParticipantIdentity(room=room_name, identity=identity))
    try:
        await lk.room.delete_room(DeleteRoomRequest(room=room_name))
    except Exception:
        pass


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
async def upload_contacts_excel(file: UploadFile = File(...)):
    """Ersetzt die Kontakt-Excel-Datei für das Web-UI."""
    filename = (file.filename or "").lower()
    if not filename.endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="Bitte eine .xlsx Datei hochladen")

    target_path = os.getenv("CONTACTS_EXCEL_PATH", "contacts.xlsx")
    target_dir = os.path.dirname(target_path)
    if target_dir:
        os.makedirs(target_dir, exist_ok=True)

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Datei ist leer")

    with open(target_path, "wb") as f:
        f.write(content)

    try:
        from contacts_excel import load_contacts
        contacts = load_contacts(target_path)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Datei gespeichert, aber nicht lesbar: {e}")

    return {
        "status": "ok",
        "path": target_path,
        "count": len(contacts),
    }


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
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    if not phone:
        raise HTTPException(status_code=400, detail="Telefonnummer fehlt")

    try:
        from contacts_excel import normalize_phone
        phone = normalize_phone(phone)
    except Exception:
        phone = str(phone).strip()

    if phone and not phone.startswith("+"):
        digits_only = re.sub(r"\D", "", phone)
        phone = f"+{digits_only}" if digits_only else phone

    if not re.fullmatch(r"\+[1-9]\d{7,14}", phone or ""):
        raise HTTPException(
            status_code=400,
            detail="Telefonnummer ungültig. Bitte im internationalen Format angeben (z.B. +491701234567).",
        )

    if not SIP_TRUNK_ID:
        raise HTTPException(status_code=500, detail="LIVEKIT_SIP_TRUNK_ID nicht konfiguriert")

    try:
        from livekit.api import CreateAgentDispatchRequest, CreateSIPParticipantRequest

        room_name = f"call-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"

        async with LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET) as lk:
            await _cleanup_stale_call_rooms(lk)

            # Agent dispatchen
            dispatch = await lk.agent_dispatch.create_dispatch(
                CreateAgentDispatchRequest(agent_name=AGENT_NAME, room=room_name)
            )

            # SIP-Anruf starten
            participant = await lk.sip.create_sip_participant(
                CreateSIPParticipantRequest(
                    sip_trunk_id=SIP_TRUNK_ID,
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
            call_sid = participant.sip_call_id
            _active_calls[call_sid] = {
                "room": room_name,
                "to": phone,
                "status": "in-progress",
                "created_at": datetime.datetime.now().isoformat(),
            }
            return {
                "status": "calling",
                "to": phone,
                "room": room_name,
                "call_sid": call_sid,
                "sip_call_id": participant.sip_call_id,
            }
    except Exception as e:
        logger.error("Anruf fehlgeschlagen: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/twilio/hangup")
async def hangup_call(req: dict):
    call_sid = str(req.get("call_sid", "")).strip()
    if not call_sid:
        raise HTTPException(status_code=400, detail="'call_sid' fehlt")

    call_info = _active_calls.get(call_sid)
    if not call_info:
        return {"call_sid": call_sid, "status": "completed"}

    room_name = call_info.get("room", "")
    try:
        async with LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET) as lk:
            if room_name:
                await _hangup_livekit_call(lk, room_name)
        call_info["status"] = "completed"
        return {"call_sid": call_sid, "status": "completed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/twilio/call-status/{call_sid}")
async def call_status(call_sid: str):
    call_sid = str(call_sid or "").strip()
    if not call_sid:
        raise HTTPException(status_code=400, detail="'call_sid' fehlt")

    call_info = _active_calls.get(call_sid)
    if not call_info:
        return {"call_sid": call_sid, "status": "completed", "to": ""}

    room_name = call_info.get("room", "")
    status = call_info.get("status", "completed")
    if room_name and status != "completed":
        try:
            async with LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET) as lk:
                rooms_response = await lk.room.list_rooms(ListRoomsRequest(names=[room_name]))
                rooms = getattr(rooms_response, "rooms", [])
                if not rooms:
                    status = "completed"
                else:
                    participants_response = await lk.room.list_participants(
                        ListParticipantsRequest(room=room_name)
                    )
                    participants = getattr(participants_response, "participants", []) or []
                    active_remote = [
                        p for p in participants
                        if not str(getattr(p, "identity", "")).startswith("agent-")
                    ]
                    if not active_remote:
                        status = "completed"
                    else:
                        status = "in-progress"
                if status == "completed":
                    try:
                        await _hangup_livekit_call(lk, room_name)
                    except Exception:
                        pass
                    call_info["status"] = "completed"
        except Exception:
            status = call_info.get("status", "in-progress")

    if status == "completed":
        call_info["status"] = "completed"

    return {
        "call_sid": call_sid,
        "status": status,
        "to": call_info.get("to", ""),
    }


@app.get("/twilio/call-history")
async def call_history(limit: int = 5):
    if not os.path.isdir(_sessions_dir):
        return {"count": 0, "items": []}

    limit = max(1, min(limit, 100))
    files = sorted(glob.glob(os.path.join(_sessions_dir, "session_*.md")), reverse=True)[:limit]
    items = [_parse_session_report(path) for path in files]
    return {"count": len(items), "items": items}


@app.get("/twilio/call-history/{session_id}")
async def call_history_detail(session_id: str):
    safe_id = re.sub(r"[^0-9_]", "", session_id or "")
    if not safe_id:
        raise HTTPException(status_code=400, detail="Ungültige Session-ID")

    session_path = os.path.join(_sessions_dir, f"session_{safe_id}.md")
    if not os.path.exists(session_path):
        raise HTTPException(status_code=404, detail="Session nicht gefunden")

    return _parse_session_report(session_path)


# Frontend ausliefern
@app.get("/")
async def serve_frontend():
    return FileResponse(os.path.join(os.path.dirname(__file__), "frontend", "index.html"))


if os.path.isdir(_sessions_dir):
    app.mount("/media/sessions", StaticFiles(directory=_sessions_dir), name="sessions-media")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
