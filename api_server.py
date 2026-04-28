"""
Einfacher API-Server für das LaVita Terminagent Frontend.
Stellt Endpoints für Kontakte und Anrufe via LiveKit SIP bereit.

Starten: python api_server.py
"""

import asyncio
import datetime
import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

LIVEKIT_URL = os.getenv("LIVEKIT_URL", "")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "")
SIP_TRUNK_ID = os.getenv("LIVEKIT_SIP_TRUNK_ID", "")
AGENT_NAME = os.getenv("LIVEKIT_AGENT_NAME", "lavita-agent")

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
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    if not phone:
        raise HTTPException(status_code=400, detail="Telefonnummer fehlt")

    if not SIP_TRUNK_ID:
        raise HTTPException(status_code=500, detail="LIVEKIT_SIP_TRUNK_ID nicht konfiguriert")

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
            return {
                "status": "calling",
                "to": phone,
                "room": room_name,
                "sip_call_id": participant.sip_call_id,
            }
    except Exception as e:
        logger.error("Anruf fehlgeschlagen: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


# Frontend ausliefern
@app.get("/")
async def serve_frontend():
    return FileResponse(os.path.join(os.path.dirname(__file__), "frontend", "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
