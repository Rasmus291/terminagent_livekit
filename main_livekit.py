import asyncio
import datetime
import logging
import os
import sys
import threading
import time

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, RunContext, function_tool
from livekit.plugins import google, silero

from config import GEMINI_API_KEY, MODEL_ID, SYSTEM_INSTRUCTION
from reporting_livekit import build_learning_brief, generate_analysis, save_session_report
from tool_handler_livekit import (
    call_ended,
    check_availability,
    crm_data_saved,
    end_call,
    mark_assistant_farewell,
    mark_partner_farewell,
    reset_call_state,
    schedule_appointment,
)

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_START_TRIGGER_PREFIX = "[START_TRIGGER]"


class LaVitaLiveKitAgent(Agent):
    def __init__(self, instructions: str):
        super().__init__(instructions=instructions)

    @function_tool()
    async def check_availability(self, context: RunContext, days_ahead: int = 5) -> dict:
        """Prüft verfügbare Terminslots in Calendly für die nächsten Tage (1-7)."""
        return await check_availability(days_ahead=days_ahead)

    @function_tool()
    async def schedule_appointment(
        self,
        context: RunContext,
        partner_name: str,
        status: str,
        appointment_date: str = "",
        contact_method: str = "",
        notes: str = "",
    ) -> dict:
        """Speichert Termindaten, erstellt optional einen Calendly-Link und versendet eine Benachrichtigung."""
        context.disallow_interruptions()
        return await schedule_appointment(
            partner_name=partner_name,
            status=status,
            appointment_date=appointment_date,
            contact_method=contact_method,
            notes=notes,
        )

    @function_tool()
    async def end_call(self, context: RunContext, reason: str) -> dict:
        """Beendet das Gespräch aktiv nach der finalen Verabschiedung."""
        return await end_call(reason=reason)


server = AgentServer()


@server.rtc_session(agent_name=os.getenv("LIVEKIT_AGENT_NAME", "lavita-agent"))
async def lavita_agent(ctx: JobContext):
    if not GEMINI_API_KEY:
        logger.error("API-Key fehlt! Bitte GEMINI_API_KEY in der .env setzen.")
        return

    os.makedirs("sessions", exist_ok=True)
    reset_call_state()

    session_transcript: list[str] = []
    session_start_time = datetime.datetime.now()
    session_start_perf = time.perf_counter()
    session_timestamp = session_start_time.strftime("%Y%m%d_%H%M%S")
    assistant_started_event = asyncio.Event()

    learning_brief = build_learning_brief(max_sessions=20)
    runtime_instruction = SYSTEM_INSTRUCTION
    if learning_brief:
        runtime_instruction = f"{SYSTEM_INSTRUCTION}\n\n{learning_brief}"
        logger.info("Lernkontext aus vergangenen Sessions geladen.")
    runtime_instruction = (
        f"{runtime_instruction}\n\n"
        "AKTUELLER KONTEXT: Der Partner ist bereits in der Leitung. "
        "Beginne jetzt proaktiv mit Begrüßung und kurzem Anliegen. "
        "Mache noch keinen konkreten Terminslot in der ersten Aussage."
    )

    agent = LaVitaLiveKitAgent(instructions=runtime_instruction)
    session = AgentSession(
        llm=google.realtime.RealtimeModel(
            model=MODEL_ID,
            voice=os.getenv("LIVEKIT_GEMINI_VOICE", "Kore"),
            api_key=GEMINI_API_KEY,
            instructions=runtime_instruction,
            language="de-DE",
        ),
        vad=silero.VAD.load(),
    )

    def on_conversation_item(event):
        item = getattr(event, "item", None)
        if not item:
            logger.debug("on_conversation_item: Item ist None")
            return
        role = getattr(item, "role", None)
        text = getattr(item, "text_content", None)
        if not text:
            logger.debug(f"on_conversation_item: Text ist None (role={role})")
            return

        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if role == "user":
            if text.startswith(_START_TRIGGER_PREFIX):
                logger.info("Interner Start-Trigger gesendet.")
                return
            logger.info("User: %s", text)
            session_transcript.append(f"**[{ts}] User:** {text}")
            logger.info("Rufe mark_partner_farewell auf mit: '%s'", text)
            mark_partner_farewell(text)
        elif role == "assistant":
            assistant_started_event.set()
            logger.info("Agent: %s", text)
            session_transcript.append(f"**[{ts}] Agent:** {text}")
            mark_assistant_farewell(text)

    session.on("conversation_item_added", on_conversation_item)

    async def finalize_session(reason: str = ""):
        call_duration = time.perf_counter() - session_start_perf
        call_start_str = session_start_time.strftime("%Y-%m-%d %H:%M:%S")

        logger.info("Generiere Analyse + Sentiment...")
        try:
            analysis = await asyncio.to_thread(generate_analysis, session_transcript)
        except Exception as e:
            logger.error("Analyse fehlgeschlagen: %s", e, exc_info=True)
            analysis = {
                "zusammenfassung": f"*Analyse-Fehler: {e}*",
                "sentiment_partner": None,
                "sentiment_gesamt": "unbekannt",
                "stimmung_details": "",
                "ergebnis": "unbekannt",
            }

        logger.info("Speichere Session Report...")
        save_session_report(
            session_transcript,
            crm_data=crm_data_saved or None,
            call_duration=call_duration,
            call_start_time=call_start_str,
            analysis=analysis,
            timestamp=session_timestamp,
        )

        # E-Mail mit Gesprächsergebnis + Analyse versenden
        if crm_data_saved:
            import email_service

            email_service.send_appointment_proposal(
                partner_name=crm_data_saved.get("partner_name", "Unbekannt"),
                appointment_date=crm_data_saved.get("appointment_date", ""),
                notes=crm_data_saved.get("notes", ""),
                status=crm_data_saved.get("status", "unbekannt"),
                calendly_link=crm_data_saved.get("calendly_link"),
                analysis=analysis,
            )

        if reason:
            logger.info("Session beendet (%s).", reason)

    ctx.add_shutdown_callback(finalize_session)

    logger.info("Verbinde LiveKit Room + Gemini Live Modell...")
    await ctx.connect()
    job_id = str(getattr(getattr(ctx, "job", None), "id", ""))
    is_console_job = job_id.startswith("mock-job")
    participant_wait_timeout = float(os.getenv("LIVEKIT_WAIT_PARTICIPANT_SECS", "45"))
    if is_console_job:
        logger.info("Console/Mock-Job erkannt (%s) – starte ohne Teilnehmer-Wartezeit.", job_id)
    else:
        try:
            logger.info("Warte auf verbundenen Teilnehmer (timeout=%.0fs)...", participant_wait_timeout)
            participant = await asyncio.wait_for(ctx.wait_for_participant(), timeout=participant_wait_timeout)
            logger.info("Teilnehmer verbunden: identity=%s", participant.identity)
        except asyncio.TimeoutError:
            logger.warning("Kein Teilnehmer innerhalb von %.0fs erkannt. Starte trotzdem Session.", participant_wait_timeout)

    async def end_call_monitor():
        """Überwacht ob end_call() aufgerufen wurde und beendet dann die Session."""
        try:
            logger.info("End-Call Monitor aktiviert. Warte auf Auflegen-Signal...")
            # Timeout auf 10 Minuten setzen um zu lange Calls zu vermeiden
            await asyncio.wait_for(call_ended.wait(), timeout=600)
            logger.info("End-Call Signal empfangen!")
            logger.info("Fahre Session herunter (SOFORT - drain=False)...")
            
            # Immediately shutdown session
            session.shutdown(drain=False)
            logger.info("Session sofort beendet - Audio-Aufnahme gestoppt.")
            
            # Schedule forced exit in background thread after brief delay
            def force_exit_later():
                time.sleep(1)
                logger.info("🛑 FORCE EXIT - Beende Prozess jetzt...")
                os._exit(0)
            
            exit_thread = threading.Thread(target=force_exit_later, daemon=True)
            exit_thread.start()
            return
        except asyncio.TimeoutError:
            logger.warning("Call timeout nach 2 Stunden erreicht. Fahre Session herunter.")
            session.shutdown(drain=False)
            return
        except Exception as e:
            logger.error(f"Fehler im End-Call Monitor: {e}", exc_info=True)
            session.shutdown(drain=False)
            return

    async def run_session():
        """Startet die Session und stößt die Gesprächseröffnung genau einmal aktiv an."""
        logger.info("Starte Gemini Live Session...")
        try:
            await session.start(room=ctx.room, agent=agent)
            logger.info("Session erfolgreich gestartet.")
        except Exception as e:
            logger.error(f"Fehler beim Session-Start: {e}", exc_info=True)
            raise

        # Warte bis Gemini-WebSocket vollständig aufgebaut ist
        await asyncio.sleep(3.0)
        try:
            logger.info("Stoße Gesprächseröffnung einmalig an...")
            session.generate_reply(
                user_input=f"{_START_TRIGGER_PREFIX} Beginne jetzt das Gespräch.",
            )
        except Exception as e:
            logger.warning("Gesprächseröffnung per generate_reply() fehlgeschlagen: %s", e)

    # Starte Session und End-Call Monitor parallel
    logger.info("Starte Agent-Loop (session + end_call_monitor)...")
    try:
        await asyncio.gather(
            run_session(),
            end_call_monitor(),
            return_exceptions=False
        )
    except Exception as e:
        logger.error(f"Fehler im Agent-Loop: {e}", exc_info=True)
    finally:
        logger.info("Agent-Loop beendet.")


if __name__ == "__main__":
    agents.cli.run_app(server)
