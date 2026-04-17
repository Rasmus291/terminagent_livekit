import os
import asyncio
import logging
import datetime
import time

from google import genai
from google.genai import types

from config import GEMINI_API_KEY, MODEL_ID, LIVE_CONFIG
from audio_handler import AudioStreamer
from reporting import save_session_report, generate_summary
from tool_handler import process_tool_calls

# Logging Setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


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
    current_turn_agent_text = ""

    client = genai.Client(api_key=GEMINI_API_KEY)
    audio_streamer = AudioStreamer()

    try:
        logger.info("Verbinde mit Gemini Live API...")
        async with client.aio.live.connect(model=MODEL_ID, config=LIVE_CONFIG) as session:
            logger.info("Session gestartet. Du kannst jetzt sprechen.")
            audio_streamer.start()

            last_audio_sent_time = None

            async def send_audio():
                nonlocal last_audio_sent_time
                async for chunk in audio_streamer.get_input_stream():
                    await session.send_realtime_input(audio=types.Blob(
                        mime_type="audio/pcm;rate=16000",
                        data=chunk
                    ))
                    last_audio_sent_time = time.perf_counter()

            async def trigger_greeting():
                """Sendet einen initialen Trigger, damit der Agent das Gespräch eröffnet."""
                await asyncio.sleep(0.5)  # Kurz warten bis receive_responses läuft
                logger.info("Sende Begrüßungs-Trigger...")
                await session.send_client_content(
                    turns=types.Content(role="user", parts=[types.Part.from_text(
                        text="Der Partner hat gerade abgenommen. Begrüße ihn jetzt und starte das Gespräch."
                    )]),
                    turn_complete=True
                )

            async def receive_responses():
                nonlocal last_audio_sent_time, current_turn_agent_text
                while True:
                    turn = session.receive()
                    current_turn_agent_text = ""
                    first_audio_in_turn = True

                    async for response in turn:
                        # Server Content verarbeiten
                        sc = response.server_content
                        if sc is not None:
                            if getattr(sc, 'interrupted', False):
                                logger.info("Agent unterbrochen.")
                                audio_streamer.clear_output()

                            # User-Transkription
                            if getattr(sc, 'input_transcription', None) and getattr(sc.input_transcription, 'text', None):
                                text = sc.input_transcription.text
                                ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                logger.info(f"User: {text}")
                                session_transcript.append(f"**[{ts}] User:** {text}")

                            # Agent-Transkription sammeln
                            if getattr(sc, 'output_transcription', None) and getattr(sc.output_transcription, 'text', None):
                                current_turn_agent_text += sc.output_transcription.text

                            # Audio-Wiedergabe + Latenz-Messung
                            if getattr(sc, 'model_turn', None) is not None:
                                for part in sc.model_turn.parts:
                                    if getattr(part, 'inline_data', None):
                                        if first_audio_in_turn and last_audio_sent_time is not None:
                                            latency_ms = (time.perf_counter() - last_audio_sent_time) * 1000
                                            latency_measurements.append(latency_ms)
                                            avg = sum(latency_measurements) / len(latency_measurements)
                                            logger.info(f"⚡ Latenz: {latency_ms:.0f}ms | Ø {avg:.0f}ms (n={len(latency_measurements)})")
                                            first_audio_in_turn = False
                                        audio_streamer.play_output_stream(part.inline_data.data)

                            # Turn abgeschlossen → Agent-Text sichern
                            if getattr(sc, 'turn_complete', False):
                                if current_turn_agent_text.strip():
                                    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                                    logger.info(f"Agent: {current_turn_agent_text}")
                                    session_transcript.append(f"**[{ts}] Agent:** {current_turn_agent_text}")
                                current_turn_agent_text = ""

                        # Tool Calls verarbeiten
                        should_end = await process_tool_calls(
                            response, session, crm_data_saved, audio_streamer
                        )
                        if should_end:
                            raise asyncio.CancelledError("Call completed")

            await asyncio.gather(send_audio(), receive_responses(), trigger_greeting())

    except asyncio.CancelledError:
        logger.info("Session beendet.")
    except Exception as e:
        logger.error(f"Fehler in der Live-Session: {e}", exc_info=True)
    finally:
        audio_streamer.stop()

        # Unvollständigen Agent-Turn noch sichern
        if current_turn_agent_text.strip():
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            session_transcript.append(f"**[{ts}] Agent:** {current_turn_agent_text}")

        call_duration = time.perf_counter() - session_start_perf
        call_start_str = session_start_time.strftime('%Y-%m-%d %H:%M:%S')

        logger.info("Generiere Zusammenfassung...")
        summary = generate_summary(client, session_transcript)

        logger.info("Speichere Session Report...")
        save_session_report(
            session_transcript,
            crm_data=crm_data_saved or None,
            latency_data=latency_measurements,
            call_duration=call_duration,
            call_start_time=call_start_str,
            summary=summary
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Beendet durch Benutzer (Ctrl+C).")
