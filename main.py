"""LiveKit Agent Entry Point — Orchestriert Session-Lifecycle."""

import asyncio
import datetime
import logging
import os
import time

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import Agent, AgentServer, AgentSession, JobContext
from livekit.plugins import google, silero
from google.genai import types as genai_types

from audio_recorder import RoomAudioRecorder
from config import GEMINI_API_KEY, MODEL_ID, SYSTEM_INSTRUCTION
from session_manager import (
    _START_TRIGGER_PREFIX,
    create_conversation_handler,
    end_call_monitor,
    finalize_session,
    register_audio_latency_events,
)
from tool_handler import (
    call_ended,
    crm_data_saved,
    mark_assistant_farewell,
    mark_partner_farewell,
    reset_call_state,
)

load_dotenv()
logger = logging.getLogger(__name__)


class LaVitaLiveKitAgent(Agent):
    def __init__(self, instructions: str):
        super().__init__(instructions=instructions)


server = AgentServer(num_idle_processes=1)


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

    # Calendly parallel laden
    async def _load_calendly():
        try:
            import calendly_service
            slots = await calendly_service.format_available_slots(days_ahead=5)
            if slots and "Keine freien" not in slots:
                return f"\n\nVERFÜGBARE TERMINE (vorab geladen):\n{slots}"
        except Exception:
            pass
        return ""

    calendly_task = asyncio.create_task(_load_calendly())

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

    # ctx.connect() und Calendly parallel
    await ctx.connect()
    recorder.start(ctx.room)

    # Calendly mit Timeout — maximal 1s warten, sonst ohne Slots starten
    try:
        cached_slots = await asyncio.wait_for(calendly_task, timeout=1.0)
    except asyncio.TimeoutError:
        cached_slots = ""
        logger.warning("Calendly nicht rechtzeitig geladen — starte ohne Slots")
    instructions = f"{SYSTEM_INSTRUCTION}{cached_slots}"

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
                    startOfSpeechSensitivity=genai_types.StartSensitivity.START_SENSITIVITY_HIGH,
                    endOfSpeechSensitivity=genai_types.EndSensitivity.END_SENSITIVITY_HIGH,
                    silenceDurationMs=250, prefixPaddingMs=100,
                ),
            ),
        ),
        vad=silero.VAD.load(
            min_silence_duration=0.25, min_speech_duration=0.2,
            prefix_padding_duration=0.15, activation_threshold=0.7,
        ),
        turn_handling={
            "turn_detection": "realtime_llm",
            "endpointing": {"mode": "dynamic", "min_delay": 0.1, "max_delay": 0.4},
        },
        min_interruption_duration=0.8,
        min_interruption_words=2,
        false_interruption_timeout=1.0,
        aec_warmup_duration=0.0,
    )

    # Event-Handler registrieren
    handler = create_conversation_handler(transcript, latencies, started_event, {
        "mark_partner_farewell": mark_partner_farewell,
        "mark_assistant_farewell": mark_assistant_farewell,
    })
    session.on("conversation_item_added", handler)
    register_audio_latency_events(session)

    def on_close(event):
        if not call_ended.is_set():
            call_ended.set()
    session.on("close", on_close)

    # Session sofort starten (verbindet zu Gemini) — parallel zum Warten auf Partner
    session_start_task = asyncio.create_task(session.start(room=ctx.room, agent=agent))

    job_id = str(getattr(getattr(ctx, "job", None), "id", ""))
    partner_name = ""
    if not job_id.startswith("mock-job"):
        timeout = float(os.getenv("LIVEKIT_WAIT_PARTICIPANT_SECS", "45"))
        try:
            participant = await asyncio.wait_for(ctx.wait_for_participant(), timeout=timeout)
            pname = getattr(participant, "name", "") or ""
            logger.info("Participant name: '%s'", pname)
            if pname.startswith("Partner (") and pname.endswith(")"):
                extracted = pname[9:-1]
                if not extracted.startswith("+") and not extracted.startswith("phone-"):
                    partner_name = extracted
            elif pname and not pname.startswith("phone-") and not pname.startswith("+"):
                partner_name = pname
        except asyncio.TimeoutError:
            logger.warning("Kein Teilnehmer innerhalb von %.0fs.", timeout)

    # notify_call_start mit contact_name (nach Participant-Erkennung)
    asyncio.create_task(recorder.notify_call_start(contact_name=partner_name))

    # Sicherstellen dass Session bereit ist
    await session_start_task
    logger.info("Gemini-Session bereit — starte Begrüßung sofort.")

    async def run():
        # Kurze Pause damit der Partner "Hallo?" sagen kann
        await asyncio.sleep(0.5)
        name_hint = f" Der Partner heißt {partner_name}. Begrüße ihn mit 'Hallo Herr/Frau {partner_name}, hier ist Anna von LaVita.'" if partner_name else ""
        trigger = f"{_START_TRIGGER_PREFIX} Der Partner hat abgenommen.{name_hint} Begrüße ihn jetzt freundlich und erkläre kurz dein Anliegen."
        logger.info("Sende Gesprächseröffnung: %s", trigger[:80])
        try:
            session.generate_reply(user_input=trigger)
        except Exception as e:
            logger.error("Gesprächseröffnung fehlgeschlagen: %s", e)

    await asyncio.gather(run(), end_call_monitor(ctx, _finalize, session))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    agents.cli.run_app(server)
