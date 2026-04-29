"""LiveKit Agent Entry Point — Orchestriert Session-Lifecycle."""

import asyncio
import datetime
import logging
import os
import time

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, RunContext, function_tool
from livekit.plugins import google, silero
from google.genai import types as genai_types

from audio_recorder import RoomAudioRecorder
from config import GEMINI_API_KEY, MODEL_ID, SYSTEM_INSTRUCTION
from session_manager import (
    _START_TRIGGER_PREFIX,
    create_conversation_handler,
    end_call_monitor,
    finalize_session,
)
from tool_handler import (
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
logger = logging.getLogger(__name__)


class LaVitaLiveKitAgent(Agent):
    def __init__(self, instructions: str):
        super().__init__(instructions=instructions)

    @function_tool()
    async def check_availability(self, context: RunContext, days_ahead: int = 5) -> dict:
        """Prüft verfügbare Terminslots in Calendly für die nächsten Tage (1-7)."""
        return await check_availability(days_ahead=days_ahead)

    @function_tool()
    async def schedule_appointment(self, context: RunContext, partner_name: str,
                                    status: str, appointment_date: str = "",
                                    contact_method: str = "", notes: str = "") -> dict:
        """Speichert Termindaten und versendet eine Benachrichtigung."""
        context.disallow_interruptions()
        return await schedule_appointment(
            partner_name=partner_name, status=status,
            appointment_date=appointment_date, contact_method=contact_method, notes=notes,
        )

    @function_tool()
    async def end_call(self, context: RunContext, reason: str) -> dict:
        """Beendet das Gespräch aktiv nach der finalen Verabschiedung."""
        return await end_call(reason=reason)


server = AgentServer()


@server.rtc_session(agent_name=os.getenv("LIVEKIT_AGENT_NAME", "lavita-agent"))
async def lavita_agent(ctx: JobContext):
    if not GEMINI_API_KEY:
        logger.error("API-Key fehlt!")
        return

    os.makedirs("sessions", exist_ok=True)
    reset_call_state()

    transcript: list[str] = []
    latencies: list[float] = []
    start_time = datetime.datetime.now()
    start_perf = time.perf_counter()
    ts = start_time.strftime("%Y%m%d_%H%M%S")
    started_event = asyncio.Event()
    recorder = RoomAudioRecorder()

    # Calendly vorab cachen
    cached = ""
    try:
        import calendly_service
        slots = await calendly_service.format_available_slots(days_ahead=5)
        if slots and "Keine freien" not in slots:
            cached = f"\n\nVERFÜGBARE TERMINE (vorab geladen):\n{slots}"
    except Exception:
        pass

    instructions = (
        f"{SYSTEM_INSTRUCTION}{cached}\n\n"
        "AKTUELLER KONTEXT: Der Partner ist bereits in der Leitung. "
        "Beginne jetzt proaktiv mit Begrüßung und kurzem Anliegen. "
        "Mache noch keinen konkreten Terminslot in der ersten Aussage."
    )

    agent = LaVitaLiveKitAgent(instructions=instructions)
    session = AgentSession(
        llm=google.realtime.RealtimeModel(
            model=MODEL_ID,
            voice=os.getenv("LIVEKIT_GEMINI_VOICE", "Kore"),
            api_key=GEMINI_API_KEY,
            instructions=instructions,
            language="de-DE",
            realtime_input_config=genai_types.RealtimeInputConfig(
                automaticActivityDetection=genai_types.AutomaticActivityDetection(
                    startOfSpeechSensitivity=genai_types.StartSensitivity.START_SENSITIVITY_LOW,
                    endOfSpeechSensitivity=genai_types.EndSensitivity.END_SENSITIVITY_HIGH,
                    silenceDurationMs=500, prefixPaddingMs=200,
                ),
            ),
        ),
        vad=silero.VAD.load(
            min_silence_duration=0.4, min_speech_duration=0.25,
            prefix_padding_duration=0.2, activation_threshold=0.7,
        ),
        turn_handling={
            "turn_detection": "realtime_llm",
            "endpointing": {"mode": "dynamic", "min_delay": 0.2, "max_delay": 0.6},
        },
        min_interruption_duration=0.8,
        min_interruption_words=2,
        false_interruption_timeout=1.0,
    )

    # Event-Handler registrieren
    handler = create_conversation_handler(transcript, latencies, started_event, {
        "mark_partner_farewell": mark_partner_farewell,
        "mark_assistant_farewell": mark_assistant_farewell,
    })
    session.on("conversation_item_added", handler)

    def on_close(event):
        if not call_ended.is_set():
            call_ended.set()
    session.on("close", on_close)

    _done = False

    async def _finalize(reason=""):
        nonlocal _done
        if _done:
            return
        _done = True
        await finalize_session(
            transcript, crm_data_saved or None, recorder,
            start_time, start_perf, ts, latencies,
        )

    ctx.add_shutdown_callback(_finalize)

    await ctx.connect()
    recorder.start(ctx.room)
    await recorder.notify_call_start()

    job_id = str(getattr(getattr(ctx, "job", None), "id", ""))
    if not job_id.startswith("mock-job"):
        timeout = float(os.getenv("LIVEKIT_WAIT_PARTICIPANT_SECS", "45"))
        try:
            await asyncio.wait_for(ctx.wait_for_participant(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("Kein Teilnehmer innerhalb von %.0fs.", timeout)

    async def run():
        await session.start(room=ctx.room, agent=agent)
        await asyncio.sleep(0.1)
        try:
            session.generate_reply(
                user_input=f"{_START_TRIGGER_PREFIX} Der Partner hat abgenommen. Begrüße ihn SOFORT.",
            )
        except Exception as e:
            logger.warning("Gesprächseröffnung fehlgeschlagen: %s", e)

    await asyncio.gather(run(), end_call_monitor(ctx, _finalize, session))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    agents.cli.run_app(server)
