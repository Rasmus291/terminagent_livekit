import os
import asyncio
import logging
import datetime
import time
import re

from google import genai
from google.genai import types

from config import GEMINI_API_KEY, MODEL_ID, LIVE_CONFIG
from audio_handler import AudioStreamer
from reporting import save_session_report, generate_analysis
from response_handler import ResponseHandler

# Logging Setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_FAREWELL_PATTERNS = (
    "tschüss",
    "auf wiedersehen",
    "auf wiederhören",
    "wiedersehen",
    "bis zum termin",
    "bis dann",
)


def _is_farewell_text(text: str) -> bool:
    if not text:
        return False
    normalized = text.lower()
    return any(pattern in normalized for pattern in _FAREWELL_PATTERNS)


def _extract_role_text(entry: str, role: str):
    match = re.match(r"^\*\*\[[^\]]+\]\s+(User|Agent):\*\*\s*(.+)$", entry.strip())
    if not match:
        return None
    found_role, text = match.group(1), match.group(2)
    if found_role != role:
        return None
    return text.strip()


def _mutual_farewell_detected(transcript: list[str]) -> bool:
    user_farewell = False
    agent_farewell = False

    for entry in reversed(transcript[-12:]):
        user_text = _extract_role_text(entry, "User")
        if user_text and _is_farewell_text(user_text):
            user_farewell = True

        agent_text = _extract_role_text(entry, "Agent")
        if agent_text and _is_farewell_text(agent_text):
            agent_farewell = True

        if user_farewell and agent_farewell:
            return True

    return False


async def main():
    if not GEMINI_API_KEY:
        logger.error("API-Key fehlt! Bitte GEMINI_API_KEY in der .env setzen.")
        return

    os.makedirs("sessions", exist_ok=True)

    # Session-Daten
    session_transcript = []
    crm_data_saved = {}
    latency_measurements = []
    session_start_time = datetime.datetime.now()
    session_start_perf = time.perf_counter()

    client = genai.Client(api_key=GEMINI_API_KEY)
    audio_streamer = AudioStreamer()
    handler = ResponseHandler(audio_streamer, session_transcript, crm_data_saved, latency_measurements)

    # Audio-Hardware parallel zum API-Connect starten
    audio_streamer.start()

    try:
        logger.info("Verbinde mit Gemini Live API...")
        async with client.aio.live.connect(model=MODEL_ID, config=LIVE_CONFIG) as session:
            logger.info("Session gestartet. Du kannst jetzt sprechen.")

            async def send_audio():
                async for chunk in audio_streamer.get_input_stream():
                    await session.send_realtime_input(audio=types.Blob(
                        mime_type="audio/pcm;rate=16000",
                        data=chunk
                    ))
                    handler.last_audio_sent_time = time.perf_counter()

            async def trigger_greeting():
                await asyncio.sleep(0.15)
                logger.info("Sende Begrüßungs-Trigger...")
                await session.send_client_content(
                    turns=types.Content(role="user", parts=[types.Part.from_text(
                        text="Der Partner hat gerade abgenommen. Begrüße ihn jetzt und starte das Gespräch."
                    )]),
                    turn_complete=True
                )

            async def receive_responses():
                appointment_done = False
                while True:
                    if _mutual_farewell_detected(session_transcript):
                        logger.info("Beidseitige Verabschiedung erkannt. Beende Gespräch sofort.")
                        raise asyncio.CancelledError("Mutual farewell detected")

                    if appointment_done:
                        # Nach Terminvereinbarung: Noch kurz auf Rückfragen warten
                        try:
                            await asyncio.wait_for(handler.process_turn(session), timeout=15.0)
                        except asyncio.TimeoutError:
                            logger.info("Keine weiteren Rückfragen. Beende Gespräch.")
                            raise asyncio.CancelledError("Call completed")
                    else:
                        tool_triggered = await handler.process_turn(session)
                        if tool_triggered:
                            appointment_done = True

            await asyncio.gather(send_audio(), receive_responses(), trigger_greeting())

    except asyncio.CancelledError:
        logger.info("Session beendet.")
    except Exception as e:
        logger.error(f"Fehler in der Live-Session: {e}", exc_info=True)
    finally:
        audio_streamer.stop()

        # Gemeinsamer Timestamp für Audio + Report
        session_timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        # Audio-Aufzeichnung speichern
        logger.info("Speichere Audio-Aufzeichnung...")
        audio_streamer.save_recording("sessions", timestamp=session_timestamp)

        # Unvollständigen Agent-Turn noch sichern
        handler.save_pending_text()

        call_duration = time.perf_counter() - session_start_perf
        call_start_str = session_start_time.strftime('%Y-%m-%d %H:%M:%S')

        logger.info("Generiere Analyse + Sentiment...")
        analysis = await generate_analysis(client, session_transcript)

        logger.info("Speichere Session Report...")
        save_session_report(
            session_transcript,
            crm_data=crm_data_saved or None,
            latency_data=latency_measurements,
            call_duration=call_duration,
            call_start_time=call_start_str,
            analysis=analysis,
            timestamp=session_timestamp
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Beendet durch Benutzer (Ctrl+C).")
