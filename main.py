import os
import asyncio
import logging
import requests
import datetime
from google import genai
from google.genai import types

from config import GEMINI_API_KEY, MODEL_ID, LIVE_CONFIG
from audio_handler import AudioStreamer

# Logging Setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

def save_session_report(transcript, crm_data=None):
    os.makedirs("sessions", exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"sessions/session_{timestamp}.md"
    
    with open(filename, "w", encoding="utf-8") as f:
        f.write("# Session Report\n\n")
        f.write(f"**Datum:** {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"**Modell:** {MODEL_ID}\n\n")
        
        if crm_data:
            f.write("## Termindaten\n")
            f.write(f"- **Partner:** {crm_data.get('partner_name', 'N/A')}\n")
            f.write(f"- **Status:** {crm_data.get('status', 'N/A')}\n")
            f.write(f"- **Termin:** {crm_data.get('appointment_date', 'N/A')}\n")
            f.write(f"- **Kontaktart:** {crm_data.get('contact_method', 'N/A')}\n")
            f.write(f"- **Notizen:** {crm_data.get('notes', 'N/A')}\n\n")
            
        f.write("## Transkript\n\n")
        if not transcript:
            f.write("*Kein Transkript vorhanden.*\n")
        else:
            for line in transcript:
                f.write(f"{line}\n\n")
            
    logger.info(f"Session Report in {filename} gespeichert.")

async def main():
    if not GEMINI_API_KEY:
        logger.error("API-Key fehlt! Bitte GEMINI_API_KEY in der .env setzen.")
        return

    # Beim Programmstart automatisch Ordner anlegen
    os.makedirs("sessions", exist_ok=True)
    
    # Datensammlung: Liste für Gesprächsbeiträge
    session_transcript = []
    crm_data_saved = {}

    logger.info("Initialisiere Google GenAI Client...")
    client = genai.Client(api_key=GEMINI_API_KEY)

    # Überprüfung der Sampling Raten in der Initialisierung (Input 16kHz / Output 24kHz)
    audio_streamer = AudioStreamer()

    try:
        logger.info("Verbinde mit Gemini Live API...")
        async with client.aio.live.connect(model=MODEL_ID, config=LIVE_CONFIG) as session:
            logger.info("Session started. Du kannst jetzt sprechen.")
            
            audio_streamer.start()

            async def send_realtime_audio():
                logger.info("Sending audio...")
                async for chunk in audio_streamer.get_input_stream():
                    await session.send_realtime_input(audio=types.Blob(
                        mime_type="audio/pcm;rate=16000",
                        data=chunk
                    ))

            async def receive_responses():
                logger.info("Receiving responses...")
                while True:
                    turn = session.receive()
                    turn_agent_text = ""
                    async for response in turn:
                        server_content = response.server_content
                        if server_content is not None:
                            if getattr(server_content, 'interrupted', False):
                                logger.info("Agent wurde von dir unterbrochen. Leere Audio-Ausgabe.")
                                audio_streamer.clear_output()

                            # User-Eingaben transkribieren
                            if getattr(server_content, 'input_transcription', None) and getattr(server_content.input_transcription, 'text', None):
                                text = server_content.input_transcription.text
                                timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                logger.info(f"User: {text}")
                                session_transcript.append(f"**[{timestamp}] User:** {text}")

                            if getattr(server_content, 'output_transcription', None) and getattr(server_content.output_transcription, 'text', None):
                                turn_agent_text += server_content.output_transcription.text

                            if getattr(server_content, 'model_turn', None) is not None:
                                for part in server_content.model_turn.parts:
                                    # Audio-Daten für Wiedergabe (24kHz)
                                    if getattr(part, 'inline_data', None):
                                        audio_streamer.play_output_stream(part.inline_data.data)
                                        
                        # Function/Tool Call verarbeiten (direkt auf dem response Objekt)
                        if getattr(response, 'tool_call', None):
                            for fc in response.tool_call.function_calls:
                                logger.info(f"Function Call empfangen: {fc.name}")
                                logger.info(f"Argumente: {fc.args}")
                                
                                if fc.name == "schedule_appointment":
                                    logger.info("Terminvereinbarung empfangen...")
                                    payload = dict(fc.args) if fc.args else {}
                                    
                                    # Termindaten für Reporting sichern und sofortigen Save triggern
                                    crm_data_saved.update(payload)
                                    save_session_report(session_transcript, crm_data=payload)

                                    # TODO: Hier später Twilio / Google Calendar / Calendly Anbindung
                                    # Beispiel: Twilio-Anruf beenden, Termin in Kalender eintragen
                                    logger.info(f"Termin-Status: {payload.get('status', 'unbekannt')}")
                                    logger.info(f"Partner: {payload.get('partner_name', 'unbekannt')}")
                                    if payload.get('appointment_date'):
                                        logger.info(f"Termin: {payload.get('appointment_date')}")

                                    logger.info("Sende Tool Response zurück...")
                                    try:
                                        await session.send_tool_response(
                                            function_responses=[
                                                types.FunctionResponse(
                                                    id=fc.id,
                                                    name=fc.name,
                                                    response={"status": "recorded"}
                                                )
                                            ]
                                        )
                                    except Exception as send_err:
                                        logger.error(f"Konnte Tool Response nicht an Modell zurücksenden: {send_err}")
                                        
                                    logger.info("Termin verarbeitet. Gespräch wird beendet...")
                                    
                                    # Wait for any pending concluding audio to finish playing
                                    while not audio_streamer.output_queue.empty():
                                        await asyncio.sleep(0.5)
                                    await asyncio.sleep(2.0)  # small buffer for the audio player to wrap up
                                    
                                    # Gracefully terminate the call
                                    raise asyncio.CancelledError("Call completed")

                            # Wenn der Turn komplett ist, speichern wir den aggregierten Text des Agents
                            if getattr(server_content, 'turn_complete', False):
                                if turn_agent_text.strip():
                                    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                    logger.info(f"Agent: {turn_agent_text}")
                                    session_transcript.append(f"**[{timestamp}] Agent:** {turn_agent_text}")
                                turn_agent_text = ""

            # Asynchrone Tasks parallel ausführen
            await asyncio.gather(
                send_realtime_audio(),
                receive_responses()
            )

    except asyncio.CancelledError:
        logger.info("Session manuell abgebrochen.")
    except Exception as e:
        logger.error(f"Fehler in der Live-Session: {e}", exc_info=True)
    finally:
        logger.info("Beende Audio-Hardware-Ressourcen...")
        audio_streamer.stop()
        
        # Falls Session endet, ohne dass send_to_crm getriggert wurde
        if not crm_data_saved and session_transcript:
            logger.info("Speichere Report (ohne CRM Daten), da Session beendet wurde.")
            save_session_report(session_transcript, crm_data=None)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Beendet durch Benutzer (Ctrl+C).")
