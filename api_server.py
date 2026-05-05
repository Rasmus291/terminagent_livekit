"""
Einfacher API-Server für das LaVita Terminagent Frontend.
Stellt Endpoints für Kontakte und Anrufe via LiveKit SIP bereit.

Starten: python api_server.py
"""

import asyncio
import datetime
import glob
import json
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
_calls_cache_path = os.path.join(_sessions_dir, "call_index.json")


def _load_calls_cache() -> None:
    global _active_calls
    try:
        if not os.path.exists(_calls_cache_path):
            return
        with open(_calls_cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _active_calls = {str(k): v for k, v in data.items() if isinstance(v, dict)}
    except Exception as e:
        logger.warning("Konnte Call-Cache nicht laden: %s", e)


def _save_calls_cache() -> None:
    try:
        os.makedirs(_sessions_dir, exist_ok=True)
        with open(_calls_cache_path, "w", encoding="utf-8") as f:
            json.dump(_active_calls, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("Konnte Call-Cache nicht speichern: %s", e)


def _iso_to_datetime(value: str | None) -> datetime.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.datetime.fromisoformat(text)
    except Exception:
        return None


def _fmt_datetime(value: str | None) -> str:
    dt = _iso_to_datetime(value)
    if not dt:
        return ""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _fmt_duration_seconds(total_seconds: int | float) -> str:
    seconds = max(0, int(total_seconds or 0))
    minutes = seconds // 60
    rem = seconds % 60
    return f"{minutes}:{rem:02d} min"


def _parse_report_datetime(value: str) -> datetime.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _active_call_to_history_item(call_sid: str, info: dict) -> dict | None:
    session_id = str(info.get("session_id") or "").strip()
    if not session_id:
        return None

    created_at = str(info.get("created_at") or "")
    ended_at = str(info.get("ended_at") or "")
    started = _iso_to_datetime(created_at)
    ended = _iso_to_datetime(ended_at)
    if not ended:
        ended = datetime.datetime.now()
    duration_seconds = int((ended - started).total_seconds()) if started else 0

    has_conversation = bool(info.get("has_conversation"))
    if not has_conversation and duration_seconds >= 20:
        has_conversation = True

    if not has_conversation:
        return None

    partner = str(info.get("name") or info.get("to") or "").strip()
    if not _looks_like_partner_name(partner):
        partner = str(info.get("to") or "").strip()

    status = str(info.get("status") or "").strip().lower()
    if status in {"in-progress", "ringing", "queued"}:
        status_label = "läuft"
    elif status == "completed":
        status_label = "nicht erfasst"
    else:
        status_label = "nicht erfasst"

    return {
        "session_id": session_id,
        "filename": f"session_{session_id}.md",
        "date_time": _fmt_datetime(created_at),
        "duration": _fmt_duration_seconds(duration_seconds),
        "partner": partner,
        "status": status_label,
        "appointment_date": "",
        "summary": "Anruf wurde geführt. Der ausführliche Report wird noch verarbeitet.",
        "summary_preview": "Anruf wurde geführt. Der ausführliche Report wird noch verarbeitet.",
        "transcript": "(Transkript wird noch verarbeitet)",
        "audio_files": [],
        "pending": True,
        "call_sid": call_sid,
    }


def _is_stale_completed_fallback(info: dict, grace_seconds: int = 180) -> bool:
    if str(info.get("status") or "").strip().lower() != "completed":
        return False
    ended = _iso_to_datetime(str(info.get("ended_at") or ""))
    if not ended:
        return False
    age = (datetime.datetime.now() - ended).total_seconds()
    return age > grace_seconds


_load_calls_cache()

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


def _looks_like_partner_name(value: str) -> bool:
    candidate = str(value or "").strip()
    if not candidate:
        return False
    lowered = candidate.lower()
    if lowered in {"unbekannt", "partner", "n/a", "-"}:
        return False
    if any(ch in candidate for ch in ["?", "!", ":", ";"]):
        return False
    if lowered in {"ja", "ja.", "yeah", "okay", "ok", "hallo", "super"}:
        return False
    if len(candidate) < 3 or len(candidate) > 60:
        return False
    if "@" in candidate:
        return False
    if re.fullmatch(r"\+?\d[\d\s\-]{5,}", candidate):
        return False

    normalized = re.sub(r"\b(herr|frau)\b", "", lowered).strip()
    parts = [p for p in re.split(r"\s+", normalized) if p]
    if not parts or len(parts) > 3:
        return False
    if any(not re.fullmatch(r"[a-zäöüß\-']+", p) for p in parts):
        return False
    disallowed_words = {
        "worum", "geht", "gespräch", "brauchen", "bisschen", "danke", "bis", "dahin",
        "hätte", "ich", "könnte", "heute", "abend", "super",
    }
    if any(p in disallowed_words for p in parts):
        return False
    return True


def _extract_partner_name_from_transcript(transcript: str) -> str:
    text = str(transcript or "")
    patterns = [
        r"\*\*\[[^\]]+\]\s*Agent:\*\*\s*Hallo\s+(?:Herr|Frau)\s+([A-Za-zÄÖÜäöüß\-']+)",
        r"\*\*\[[^\]]+\]\s*Agent:\*\*\s*Guten\s+Tag\s+(?:Herr|Frau)\s+([A-Za-zÄÖÜäöüß\-']+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


def _extract_spoken_transcript(transcript: str) -> str:
    text = str(transcript or "")
    spoken_lines = re.findall(r"\*\*\[[^\]]+\]\s*(?:User|Agent):\*\*\s*(.+)", text)
    if spoken_lines:
        return "\n".join(line.strip() for line in spoken_lines if line and line.strip())
    return text


def _base_datetime_for_session(date_time: str, session_id: str) -> datetime.datetime | None:
    text = str(date_time or "").strip()
    if text:
        try:
            return datetime.datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except Exception:
            pass

    sid = str(session_id or "").strip()
    if re.fullmatch(r"\d{8}_\d{6}", sid):
        try:
            return datetime.datetime.strptime(sid, "%Y%m%d_%H%M%S")
        except Exception:
            return None
    return None


def _normalize_time_token(token: str) -> str:
    raw = str(token or "").strip()
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?", raw)
    if not match:
        return ""
    hour = int(match.group(1))
    minute = int(match.group(2) or "0")
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return ""
    return f"{hour:02d}:{minute:02d}"


def _infer_appointment_from_text(haystack_raw: str, base_dt: datetime.datetime | None) -> str:
    text = str(haystack_raw or "")

    month_map = {
        "januar": 1,
        "februar": 2,
        "märz": 3,
        "maerz": 3,
        "april": 4,
        "mai": 5,
        "juni": 6,
        "juli": 7,
        "august": 8,
        "september": 9,
        "oktober": 10,
        "november": 11,
        "dezember": 12,
    }

    absolute_matches = list(
        re.finditer(
            r"(?:am\s+)?(\d{1,2})\.\s*(januar|februar|märz|maerz|april|mai|juni|juli|august|september|oktober|november|dezember)(?:\s*(\d{4}))?(?:[^\.\n]{0,120}?)?(?:um\s*)?(\d{1,2}(?::\d{2})?)\s*uhr",
            text,
            re.IGNORECASE,
        )
    )
    if absolute_matches:
        m = absolute_matches[-1]
        day = int(m.group(1))
        month_name = m.group(2).lower()
        year = int(m.group(3)) if m.group(3) else (base_dt.year if base_dt else datetime.datetime.now().year)
        month = month_map.get(month_name)
        normalized_time = _normalize_time_token(m.group(4))
        if month and normalized_time:
            try:
                return datetime.datetime.strptime(f"{year:04d}-{month:02d}-{day:02d} {normalized_time}", "%Y-%m-%d %H:%M").strftime("%Y-%m-%d %H:%M")
            except Exception:
                pass

    relative_matches = list(
        re.finditer(
            r"\b(heute|morgen|übermorgen|uebermorgen)\b(?:[^\.\n]{0,120}?)?(?:um\s*)?(\d{1,2}(?::\d{2})?)\s*uhr",
            text,
            re.IGNORECASE,
        )
    )
    if relative_matches:
        m = relative_matches[-1]
        rel = m.group(1).lower()
        normalized_time = _normalize_time_token(m.group(2))
        if normalized_time:
            if not base_dt:
                return f"{normalized_time} Uhr (Datum nicht erkannt)"

            day_offset = 0
            if rel == "morgen":
                day_offset = 1
            elif rel in {"übermorgen", "uebermorgen"}:
                day_offset = 2

            target_date = (base_dt + datetime.timedelta(days=day_offset)).date()
            return f"{target_date.strftime('%Y-%m-%d')} {normalized_time}"

    same_day_matches = list(
        re.finditer(
            r"\b(am\s+selben\s+tag|noch\s+heute)\b(?:[^\.\n]{0,80}?)?(?:um\s*)?(\d{1,2}(?::\d{2})?)\s*uhr",
            text,
            re.IGNORECASE,
        )
    )
    if same_day_matches:
        m = same_day_matches[-1]
        normalized_time = _normalize_time_token(m.group(2))
        if normalized_time:
            if base_dt:
                return f"{base_dt.date().strftime('%Y-%m-%d')} {normalized_time}"
            return f"{normalized_time} Uhr (Datum nicht erkannt)"

    return ""


def _infer_status_and_appointment(
    status: str,
    appointment_date: str,
    summary: str,
    transcript: str,
    date_time: str = "",
    session_id: str = "",
) -> tuple[str, str]:
    status_value = (status or "").strip()
    appointment_value = (appointment_date or "").strip()
    if status_value.lower() in {"n/a", "na", "-", "nicht erfasst"}:
        status_value = ""
    if appointment_value.lower() in {"n/a", "na", "-"}:
        appointment_value = ""

    spoken = _extract_spoken_transcript(transcript)
    haystack_raw = f"{summary}\n{spoken}"
    haystack = haystack_raw.lower()
    base_dt = _base_datetime_for_session(date_time, session_id)

    if not status_value:
        if re.search(r"\b(termin\s+(vereinbart|bestätigt|bestaetigt|eingetragen)|bestätige\s+ich\s+den\s+termin|dann\s+sprechen\s+wir|passt\s+perfekt)\b", haystack):
            status_value = "bestätigt"
        elif re.search(r"\b(ja|super|passt|okay|ok)\b", haystack) and re.search(r"\b(termin|uhr|heute|morgen|übermorgen|uebermorgen|montag|dienstag|mittwoch|donnerstag|freitag)\b", haystack):
            status_value = "bestätigt"
        elif re.search(r"\b(kein\s+termin|nicht\s+vereinbart|abgebrochen|auflegen|keine\s+zeit)\b", haystack):
            status_value = "offen"
        elif re.search(r"\b(offen|unklar|kein\s+termin|nicht\s+vereinbart)\b", haystack):
            status_value = "offen"
        else:
            status_value = "nicht erfasst"

    if not appointment_value:
        iso_match = re.search(r"\b(20\d{2}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?)\b", haystack_raw)
        if iso_match:
            appointment_value = iso_match.group(1).replace("T", " ")
        else:
            de_match = re.search(r"\b(\d{2}\.\d{2}\.20\d{2}\s+\d{2}:\d{2})\b", haystack_raw)
            if de_match:
                appointment_value = de_match.group(1)
            else:
                inferred = _infer_appointment_from_text(haystack_raw, base_dt)
                if inferred:
                    appointment_value = inferred
                else:
                    time_matches = re.findall(r"\b(\d{1,2}:\d{2})\b", haystack)
                    if time_matches and status_value == "bestätigt":
                        appointment_value = f"{time_matches[-1]} Uhr (Datum nicht erkannt)"

    if appointment_value and status_value == "nicht erfasst":
        status_value = "bestätigt"

    return status_value, appointment_value


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

    if not _looks_like_partner_name(partner):
        guessed = _extract_partner_name_from_transcript(transcript)
        if _looks_like_partner_name(guessed):
            partner = guessed

    if not _looks_like_partner_name(partner):
        user_match = re.search(r"\*\*\[[^\]]+\]\s*User:\*\*\s*(.+)", transcript)
        if user_match:
            fallback = user_match.group(1).strip()[:80]
            partner = fallback if _looks_like_partner_name(fallback) else ""

    status, appointment_date = _infer_status_and_appointment(
        status,
        appointment_date,
        summary,
        transcript,
        date_time=date_time,
        session_id=session_id,
    )

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


def _has_real_conversation(item: dict) -> bool:
    transcript = str(item.get("transcript") or "").strip()
    if not transcript:
        return False
    if "Kein Transkript" in transcript:
        return False
    has_role_marker = ("**[" in transcript and "User:**" in transcript) or ("Agent:**" in transcript)
    if not has_role_marker:
        return False

    partner = str(item.get("partner") or "").strip().lower()
    if not _looks_like_partner_name(partner):
        return False
    return True


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
    contact = None

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

    if not contact:
        try:
            from contacts_excel import find_contact
            contact = find_contact(phone=phone)
        except Exception:
            contact = None

    resolved_name = (req.name or "").strip() or str((contact or {}).get("name") or "").strip() or phone

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
                    participant_name=f"Partner ({resolved_name})",
                    wait_until_answered=True,
                    play_ringtone=True,
                    max_call_duration={"seconds": 600},
                )
            )

            logger.info("Anruf gestartet: %s → Room %s", phone, room_name)
            call_sid = participant.sip_call_id
            now_iso = datetime.datetime.now().isoformat()
            session_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            _active_calls[call_sid] = {
                "room": room_name,
                "to": phone,
                "name": resolved_name,
                "status": "in-progress",
                "created_at": now_iso,
                "session_id": session_id,
                "has_conversation": False,
            }
            _save_calls_cache()
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
        call_info["ended_at"] = datetime.datetime.now().isoformat()
        started = _iso_to_datetime(str(call_info.get("created_at") or ""))
        if started and (datetime.datetime.now() - started).total_seconds() >= 20:
            call_info["has_conversation"] = True
        _save_calls_cache()
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
                    call_info["ended_at"] = datetime.datetime.now().isoformat()
                    started = _iso_to_datetime(str(call_info.get("created_at") or ""))
                    if started and (datetime.datetime.now() - started).total_seconds() >= 20:
                        call_info["has_conversation"] = True
                    _save_calls_cache()
        except Exception:
            status = call_info.get("status", "in-progress")

    if status == "completed":
        call_info["status"] = "completed"
        call_info.setdefault("ended_at", datetime.datetime.now().isoformat())
        started = _iso_to_datetime(str(call_info.get("created_at") or ""))
        if started and (datetime.datetime.now() - started).total_seconds() >= 20:
            call_info["has_conversation"] = True
        _save_calls_cache()

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
    files = sorted(glob.glob(os.path.join(_sessions_dir, "session_*.md")), reverse=True)
    parsed = [_parse_session_report(path) for path in files]
    filtered = [item for item in parsed if _has_real_conversation(item)]

    existing_ids = {str(item.get("session_id") or "") for item in filtered}
    parsed_keys: list[tuple[str, datetime.datetime]] = []
    for item in filtered:
        partner_key = str(item.get("partner") or "").strip().lower()
        parsed_dt = _parse_report_datetime(str(item.get("date_time") or ""))
        if partner_key and parsed_dt:
            parsed_keys.append((partner_key, parsed_dt))

    fallback_items = []
    stale_call_sids: list[str] = []
    for call_sid, call_info in _active_calls.items():
        if _is_stale_completed_fallback(call_info):
            stale_call_sids.append(call_sid)
            continue

        fallback_item = _active_call_to_history_item(call_sid, call_info)
        if not fallback_item:
            continue
        if fallback_item["session_id"] in existing_ids:
            continue

        fallback_partner = str(fallback_item.get("partner") or "").strip().lower()
        fallback_dt = _parse_report_datetime(str(fallback_item.get("date_time") or ""))
        if fallback_partner and fallback_dt:
            near_duplicate = any(
                p == fallback_partner and abs((d - fallback_dt).total_seconds()) <= 180
                for p, d in parsed_keys
            )
            if near_duplicate:
                continue

        fallback_items.append(fallback_item)

    if stale_call_sids:
        for sid in stale_call_sids:
            _active_calls.pop(sid, None)
        _save_calls_cache()

    combined = filtered + fallback_items
    combined.sort(key=lambda item: str(item.get("session_id") or ""), reverse=True)
    items = combined[:limit]
    return {"count": len(items), "items": items}


@app.get("/twilio/call-history/{session_id}")
async def call_history_detail(session_id: str):
    safe_id = re.sub(r"[^0-9_]", "", session_id or "")
    if not safe_id:
        raise HTTPException(status_code=400, detail="Ungültige Session-ID")

    session_path = os.path.join(_sessions_dir, f"session_{safe_id}.md")
    if not os.path.exists(session_path):
        for call_sid, call_info in _active_calls.items():
            if str(call_info.get("session_id") or "") == safe_id:
                fallback_item = _active_call_to_history_item(call_sid, call_info)
                if fallback_item:
                    return fallback_item
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
